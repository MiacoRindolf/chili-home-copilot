# chili-morning.ps1 -- operator morning health-check summary.
#
# Runs against the local KPI endpoint, parses the JSON, prints a
# structured colored summary with the items the operator should
# care about most. Exit code is a quick at-a-glance:
#   0 -- clean, no warnings
#   1 -- one or more warnings (drift, concentration, kill switch, etc.)
#   2 -- endpoint unreachable
#
# Usage:
#   .\scripts\chili-morning.ps1                 # local default port
#   .\scripts\chili-morning.ps1 -Port 8010      # alt port
#
# Designed to run on Windows PowerShell 5+ and PowerShell Core 7+.
# No external deps (no jq, no python). Uses ConvertFrom-Json + native
# Invoke-RestMethod with -SkipCertificateCheck for the self-signed cert.

[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$ApiBase = ""
)

$ErrorActionPreference = "Stop"

$base = if ($ApiBase) { $ApiBase } else { "https://localhost:$Port" }
$url  = "$base/api/brain/health/kpi"

# Self-signed cert. Use curl.exe (bundled with Windows 10+) since it
# handles -k cleanly across PS versions; Invoke-RestMethod's self-signed
# handling is fragile under PS 5.1.
$curl = (Get-Command curl.exe -ErrorAction SilentlyContinue).Source
if (-not $curl) {
    Write-Host "[FAIL] curl.exe not found on PATH (expected on Windows 10+)" -ForegroundColor Red
    exit 2
}
$json = & $curl -ks --max-time 15 $url 2>$null
if ($LASTEXITCODE -ne 0 -or -not $json) {
    Write-Host "[FAIL] KPI endpoint unreachable at $url" -ForegroundColor Red
    Write-Host "  curl.exe exited $LASTEXITCODE" -ForegroundColor Red
    exit 2
}
try {
    $kpi = $json | ConvertFrom-Json
} catch {
    Write-Host "[FAIL] KPI returned non-JSON" -ForegroundColor Red
    $preview = if ($json.Length -gt 200) { $json.Substring(0, 200) } else { $json }
    Write-Host "  $preview" -ForegroundColor DarkGray
    exit 2
}

$warnings = @()

function Write-Section([string]$title, [string]$color = "Cyan") {
    Write-Host ""
    Write-Host "-- $title " -NoNewline -ForegroundColor $color
    $padLen = [Math]::Max(0, 60 - $title.Length)
    Write-Host ("-" * $padLen) -ForegroundColor DarkGray
}

function Write-KV([string]$key, $value, [string]$color = "White", [int]$indent = 2) {
    $pad = " " * $indent
    Write-Host "${pad}$($key.PadRight(28))" -NoNewline -ForegroundColor DarkGray
    Write-Host "$value" -ForegroundColor $color
}

# Header
Write-Host ""
Write-Host "CHILI morning report -- $($kpi.as_of)" -ForegroundColor White

# Profitability
Write-Section "Profitability (30d)"
$prof = $kpi.profitability
$pnl  = [double]$prof.pnl_30d_usd
$pnlColor = if ($pnl -gt 0) { "Green" } elseif ($pnl -lt -500) { "Red" } else { "Yellow" }
Write-KV "trades_30d"   $prof.trades_30d
Write-KV "trades_7d"    $prof.trades_7d
Write-KV "hit_rate_30d" $prof.hit_rate_30d
Write-KV "pnl_30d_usd"  ('$' + ('{0:N2}' -f $pnl)) $pnlColor
Write-KV "worst_trade_30d_usd" ('$' + ('{0:N2}' -f [double]$prof.worst_trade_30d_usd))

# Learning
Write-Section "Learning"
$l = $kpi.learning
Write-KV "live"          $l.live
Write-KV "challenged"    $l.challenged
Write-KV "candidate"     $l.candidate
Write-KV "promoted"      $l.promoted
Write-KV "cpcv_coverage_pct" ('{0:N1}%' -f [double]$l.cpcv_coverage_pct)
if ($l.live_but_inactive -gt 0) {
    Write-KV "live_but_inactive (DRIFT WARNING)" $l.live_but_inactive "Red"
    $warnings += "lifecycle drift: $($l.live_but_inactive) patterns are live but inactive"
} else {
    Write-KV "live_but_inactive" 0 "Green"
}
Write-KV "survival_at_risk_count" $l.survival_at_risk_count
Write-KV "in_promote_review_queue" $l.in_promote_review_queue

# Diversity
Write-Section "Diversity (CHILI-attributed only)"
$d = $kpi.diversity
$hhi = [double]$d.pnl_herfindahl
$hhiColor = if ($d.concentration_warning) { "Yellow" } else { "Green" }
Write-KV "active_families" $d.active_families
Write-KV "pnl_herfindahl"  ('{0:N4}' -f $hhi) $hhiColor
if ($d.concentration_warning) {
    $warnings += "diversity: pnl_herfindahl=$hhi (>0.5, one family dominating)"
}
foreach ($f in $d.by_family_30d) {
    Write-KV ("  " + $f.family) ('$' + ('{0:N2}' -f [double]$f.pnl_30d))
}
Write-KV "external_unattributed_pnl_30d" ('$' + ('{0:N2}' -f [double]$d.external_unattributed_pnl_30d))
Write-KV "external_unattributed_trades_30d" $d.external_unattributed_trades_30d

# Manual book
if ($kpi.manual_book) {
    Write-Section "Manual book (operator trades)"
    $m = $kpi.manual_book
    $mpnl = [double]$m.pnl_30d_usd
    $mpnlColor = if ($mpnl -gt 0) { "Green" } elseif ($mpnl -lt -500) { "Red" } else { "Yellow" }
    $hr = [double]$m.hit_rate_30d
    $hitRateColor = if ($hr -lt 0.4) { "Red" } elseif ($hr -lt 0.5) { "Yellow" } else { "Green" }
    Write-KV "trades_30d"   $m.trades_30d
    Write-KV "pnl_30d_usd"  ('$' + ('{0:N2}' -f $mpnl)) $mpnlColor
    Write-KV "hit_rate_30d" ('{0:P1}' -f $hr) $hitRateColor
    Write-KV "avg_hold_hours" ('{0:N1}' -f [double]$m.avg_hold_hours)
    if ($hr -lt 0.4 -and $m.trades_30d -ge 10) {
        $warnings += ("manual book hit_rate {0:P1} on {1} trades" -f $hr, $m.trades_30d)
    }
    if ($m.top_losers -and $m.top_losers.Count -gt 0) {
        $worst = $m.top_losers[0]
        Write-KV "worst ticker" ("{0} ({1}t, `${2:N2})" -f $worst.ticker, $worst.trades, [double]$worst.pnl_usd) "Red"
    }
}

# Regime
Write-Section "Regime"
$r = $kpi.regime
if ($r.current) {
    Write-KV "current"        $r.current
    if ($r.posterior) {
        $pTop = ($r.posterior.PSObject.Properties | Sort-Object {[double]$_.Value} -Descending | Select-Object -First 1)
        Write-KV "  $($pTop.Name)" ('{0:P2}' -f [double]$pTop.Value)
    }
    Write-KV "as_of"          $r.as_of
    Write-KV "age_hours"      ('{0:N1}' -f [double]$r.age_hours)
    if ($r.stale) {
        Write-KV "stale"      "TRUE" "Yellow"
        $warnings += "regime tag stale ($($r.age_hours)h old)"
    }
} else {
    Write-KV "current" "(not yet computed)" "Yellow"
    $warnings += $r.note
}

# Safety
Write-Section "Safety"
$s = $kpi.safety
if ($s.kill_switch_active) {
    Write-KV "kill_switch_active" "TRUE" "Red"
    Write-KV "kill_switch_reason" $s.kill_switch_reason "Red"
    $warnings += "KILL SWITCH ACTIVE: $($s.kill_switch_reason)"
} else {
    Write-KV "kill_switch_active" "false" "Green"
}
Write-KV "autotrader_24h.placed" $s.autotrader_24h.placed
Write-KV "autotrader_24h.exits"  $s.autotrader_24h.exits
Write-KV "autotrader_24h.errors" $s.autotrader_24h.errors

# Footer + warnings summary
Write-Host ""
if ($warnings.Count -eq 0) {
    Write-Host "[OK] All clean." -ForegroundColor Green
    exit 0
} else {
    $plural = if ($warnings.Count -ne 1) {'s'} else {''}
    Write-Host "[WARN] $($warnings.Count) warning$plural :" -ForegroundColor Yellow
    foreach ($w in $warnings) {
        Write-Host "  * $w" -ForegroundColor Yellow
    }
    exit 1
}
