$ErrorActionPreference = "Continue"
$repo = "C:\dev\chili-home-copilot"
Set-Location $repo

$out = "$PSScriptRoot\dispatch-hypothesis-family-backfill-output.txt"
"# Hypothesis-family backfill (Phase E follow-up) — $(Get-Date -Format o)" | Out-File $out -Encoding utf8

# Clear any stale git lock so the daemon doesn't choke on us
if (Test-Path "$repo\.git\index.lock") {
    Remove-Item "$repo\.git\index.lock" -Force -ErrorAction SilentlyContinue
    "cleared stale .git/index.lock" | Add-Content $out
}

# Pre-flight: containers must be up. If chili / chili-db are down, this
# audit cannot complete (it shells into both via docker exec).
$psOut = & docker compose ps --format json 2>&1
$psOut | Add-Content $out
$haveChili = (docker ps --filter "name=chili$" --filter "status=running" -q 2>$null) -ne ""
$haveDb    = (docker ps --filter "name=chili-db" --filter "status=running" -q 2>$null) -ne ""
if (-not $haveChili -or -not $haveDb) {
    "ABORT: chili and/or chili-db not running. Run dispatch-followup-activations-recreate.ps1 first (or bring containers up), then re-queue this script." | Add-Content $out
    exit 1
}

"=== Coverage audit ===" | Add-Content $out
$audit = @"
SELECT
  COUNT(*) FILTER (WHERE hypothesis_family IS NULL)               AS null_count,
  COUNT(*) FILTER (WHERE hypothesis_family = 'unknown')           AS unknown_count,
  COUNT(*) FILTER (WHERE hypothesis_family IS NOT NULL
                     AND hypothesis_family <> 'unknown')          AS tagged_count,
  COUNT(*)                                                        AS total
FROM scan_patterns
WHERE active = true;
"@
$auditOut = docker exec chili-db psql -U chili -d chili -c $audit 2>&1
$auditOut | Add-Content $out

"=== By-family distribution (active) ===" | Add-Content $out
$dist = "SELECT COALESCE(hypothesis_family,'__NULL__') AS family, COUNT(*) FROM scan_patterns WHERE active=true GROUP BY 1 ORDER BY 2 DESC;"
docker exec chili-db psql -U chili -d chili -c $dist 2>&1 | Add-Content $out

"=== Running smoke_family_backfill.py inside chili container ===" | Add-Content $out
# Copy the smoke script into the chili container and execute it
$smoke = "$repo\scripts\_smoke_family_backfill.py"
if (Test-Path $smoke) {
    docker cp $smoke chili:/tmp/_smoke_family_backfill.py 2>&1 | Add-Content $out
    docker exec chili python /tmp/_smoke_family_backfill.py 2>&1 | Add-Content $out
} else {
    "ERROR: smoke script not found at $smoke" | Add-Content $out
}

"=== Post-backfill coverage ===" | Add-Content $out
docker exec chili-db psql -U chili -d chili -c $audit 2>&1 | Add-Content $out
docker exec chili-db psql -U chili -d chili -c $dist 2>&1 | Add-Content $out

"DONE at $(Get-Date -Format o)" | Add-Content $out
