# Extract the decay_miner pending_heap time series from
# fast-data-worker logs over the last N hours.
#
# Output: scripts/dispatch-decay-heap-trend-output.txt
# Columns: timestamp  alerts=N  heap=N  scheduled=N  finalized=N  errs=N
#
# So we can answer: is pending_heap oscillating, growing, or stable?
# Is db_errors accumulating linearly with time, or frozen?
#
# Usage:
#   .\scripts\dispatch-decay-heap-trend.ps1          # default 24h window
#   .\scripts\dispatch-decay-heap-trend.ps1 6        # 6h window
#
# F-hygiene-2 subtask 3. Diagnostic only -- no app code touched, no
# DB writes, no persisted metrics-history table. Log-grep is the
# right tool for "is this trend healthy?" until we have a brain
# consumer that needs programmatic access.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "scripts/dispatch-decay-heap-trend-output.txt"
"# decay_miner pending_heap trend $(Get-Date -Format o)" | Out-File $out -Encoding utf8

# Default 24h window; override via first arg.
$hours = if ($args.Count -gt 0) { $args[0] } else { "24" }
"---window: last ${hours}h---" | Add-Content $out

docker compose logs fast-data-worker --since "${hours}h" 2>&1 `
  | Select-String -Pattern "decay_miner alerts=" `
  | ForEach-Object {
      # Each metrics line looks like:
      #   2026-05-02 17:45:27 [INFO] ... decay_miner alerts=1051 ... pending_heap=1112 ... db_errors=13 ...
      $line = $_.ToString()
      if ($line -match "(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})") {
        $ts = $Matches[1]
        $alerts = if ($line -match "alerts=(\d+)")          { $Matches[1] } else { "?" }
        $heap   = if ($line -match "pending_heap=(\d+)")    { $Matches[1] } else { "?" }
        $sched  = if ($line -match "obs_scheduled=(\d+)")   { $Matches[1] } else { "?" }
        $final  = if ($line -match "obs_finalized=(\d+)")   { $Matches[1] } else { "?" }
        $errs   = if ($line -match "db_errors=(\d+)")       { $Matches[1] } else { "?" }
        "$ts  alerts=$alerts  heap=$heap  scheduled=$sched  finalized=$final  errs=$errs"
      }
    } `
  | Add-Content $out

Write-Output "done"
