# F-leak-1.1: long-running docker-stats logger.
#
# Runs forever. Every 60s, calls `docker stats --no-stream` once and
# appends one line per container to scripts/_stats_log/YYYY-MM-DD.txt
# (rolled daily, UTC). Intended to be left running in a side window
# or scheduled task so a 12+ hour memory-growth time series exists
# for the next leak hunt.
#
# Output line format:
#   <UTC iso> <container> mem=<MiB> cpu=<pct> netio=<bytes> blockio=<bytes> pids=<n>
#
# Usage:
#   .\scripts\dispatch-stats-logger.ps1               # default 60s interval
#   .\scripts\dispatch-stats-logger.ps1 30            # 30s interval
#
# Stop with Ctrl+C. Read the rolling log via dispatch-stats-trend.ps1.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

$intervalS = if ($args.Count -gt 0) { [int]$args[0] } else { 60 }
$logDir = "scripts\_stats_log"
if (-not (Test-Path $logDir)) {
  New-Item -ItemType Directory -Path $logDir | Out-Null
}

function Convert-MemToMiB([string]$s) {
  # docker stats prints e.g. "157.4MiB", "2.998GiB", "39MiB", "194.1MiB"
  if ($s -match "^([\d\.]+)([KMG]i?B)$") {
    $val = [double]$Matches[1]
    switch ($Matches[2]) {
      "GiB" { return [int]($val * 1024) }
      "GB"  { return [int]($val * 1024) }   # docker also emits GB sometimes
      "MiB" { return [int]$val }
      "MB"  { return [int]$val }
      "KiB" { return [int]($val / 1024) }
      "KB"  { return [int]($val / 1024) }
      default { return -1 }
    }
  }
  return -1
}

Write-Output "[stats-logger] starting; interval=${intervalS}s; log_dir=$logDir; press Ctrl+C to stop"

while ($true) {
  $tsUtc = (Get-Date).ToUniversalTime().ToString("o")
  $today = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")
  $logFile = Join-Path $logDir "$today.txt"

  $statsLines = @()
  try {
    $statsLines = docker stats --no-stream `
      --format "{{.Name}}|{{.MemUsage}}|{{.CPUPerc}}|{{.NetIO}}|{{.BlockIO}}|{{.PIDs}}" 2>&1
  } catch {
    "$tsUtc  STATS_ERROR  $_" | Add-Content -Path $logFile -Encoding utf8
    Start-Sleep -Seconds $intervalS
    continue
  }

  foreach ($l in $statsLines) {
    if ($l -notmatch "\|") { continue }   # skip headers / errors
    $parts = $l -split "\|"
    if ($parts.Count -lt 6) { continue }
    $name      = $parts[0]
    $memUsage  = $parts[1]                   # e.g. "157.4MiB / 512MiB"
    $cpuPct    = $parts[2]
    $netIo     = $parts[3]
    $blockIo   = $parts[4]
    $pids      = $parts[5]

    $memUsedRaw = $memUsage.Split('/')[0].Trim()
    $memUsedMiB = Convert-MemToMiB $memUsedRaw

    "$tsUtc  $name  mem=${memUsedMiB}MiB  cpu=$cpuPct  netio=$netIo  blockio=$blockIo  pids=$pids" `
      | Add-Content -Path $logFile -Encoding utf8
  }

  Start-Sleep -Seconds $intervalS
}
