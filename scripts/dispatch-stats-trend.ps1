# F-leak-1.1: read the rolling docker-stats log and emit per-container
# memory/CPU deltas + slopes over the last N hours.
#
# Pairs with dispatch-stats-logger.ps1, which writes to
# scripts/_stats_log/YYYY-MM-DD.txt every 60s.
#
# Output: scripts/dispatch-stats-trend-output.txt
#
# Usage:
#   .\scripts\dispatch-stats-trend.ps1            # default 1h window
#   .\scripts\dispatch-stats-trend.ps1 6          # 6h window
#   .\scripts\dispatch-stats-trend.ps1 0.5        # 30 min window

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

$hours = if ($args.Count -gt 0) { [double]$args[0] } else { 1.0 }
$out   = "scripts\dispatch-stats-trend-output.txt"
$logDir = "scripts\_stats_log"

"# stats trend $(Get-Date -Format o)" | Out-File $out -Encoding utf8
"---window: last ${hours}h---" | Add-Content $out

if (-not (Test-Path $logDir)) {
  "ERROR: log dir $logDir does not exist; start dispatch-stats-logger.ps1 first" | Add-Content $out
  Write-Output "no log dir"
  exit 1
}

$cutoffUtc = (Get-Date).ToUniversalTime().AddHours(-1.0 * $hours)

# Load the last day's logs (and the prior day if window crosses midnight).
$today     = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")
$yesterday = (Get-Date).ToUniversalTime().AddDays(-1).ToString("yyyy-MM-dd")
$files = @(
  (Join-Path $logDir "$yesterday.txt"),
  (Join-Path $logDir "$today.txt")
) | Where-Object { Test-Path $_ }

if (-not $files) {
  "ERROR: no log files found in $logDir" | Add-Content $out
  Write-Output "no log files"
  exit 1
}

# Each line: "<UTC iso>  <container>  mem=<MiB>  cpu=<pct>  netio=...  blockio=...  pids=..."
$rows = @()
foreach ($f in $files) {
  Get-Content $f | ForEach-Object {
    $line = $_
    if ($line -notmatch "^(\S+)\s+(\S+)\s+mem=(-?\d+)MiB\s+cpu=([\d\.%]+)") { return }
    $tsStr  = $Matches[1]
    $name   = $Matches[2]
    $memMiB = [int]$Matches[3]
    $cpuStr = $Matches[4].TrimEnd("%")
    try {
      $ts = [datetime]::Parse($tsStr).ToUniversalTime()
    } catch {
      return
    }
    if ($ts -lt $cutoffUtc) { return }
    $rows += [pscustomobject]@{
      Ts = $ts; Name = $name; MemMiB = $memMiB
      CpuPct = if ($cpuStr -as [double]) { [double]$cpuStr } else { 0.0 }
    }
  }
}

if (-not $rows) {
  "no rows in window" | Add-Content $out
  Write-Output "no rows"
  exit 0
}

"---per-container summary (sorted by mem-delta desc)---" | Add-Content $out
"name                                       n   mem_first   mem_last   mem_delta   cpu_avg   cpu_max   span_h" `
  | Add-Content $out

$grouped = $rows | Group-Object Name
$summary = foreach ($g in $grouped) {
  $samples = $g.Group | Sort-Object Ts
  $first   = $samples[0]
  $last    = $samples[-1]
  $cpuAvg  = ($samples | Measure-Object CpuPct -Average).Average
  $cpuMax  = ($samples | Measure-Object CpuPct -Maximum).Maximum
  $spanH   = ($last.Ts - $first.Ts).TotalHours
  [pscustomobject]@{
    Name      = $g.Name
    N         = $samples.Count
    MemFirst  = $first.MemMiB
    MemLast   = $last.MemMiB
    MemDelta  = $last.MemMiB - $first.MemMiB
    CpuAvg    = [Math]::Round($cpuAvg, 1)
    CpuMax    = [Math]::Round($cpuMax, 1)
    SpanH     = [Math]::Round($spanH, 2)
  }
}

$summary | Sort-Object MemDelta -Descending | ForEach-Object {
  ("{0,-42}  {1,4}  {2,9}  {3,8}  {4,9}  {5,7}  {6,7}  {7,6}" -f `
    $_.Name, $_.N, $_.MemFirst, $_.MemLast, $_.MemDelta, $_.CpuAvg, $_.CpuMax, $_.SpanH) `
    | Add-Content $out
}

"---tail: last 8 sample rows from log (raw)---" | Add-Content $out
$rows | Sort-Object Ts | Select-Object -Last 8 | ForEach-Object {
  ("{0}  {1,-42}  mem={2,5}MiB  cpu={3,5:n1}%" -f $_.Ts.ToString("o"), $_.Name, $_.MemMiB, $_.CpuPct) `
    | Add-Content $out
}

Write-Output "done"
