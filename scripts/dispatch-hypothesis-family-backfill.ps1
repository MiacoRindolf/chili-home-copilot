$ErrorActionPreference = "Continue"
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $repo

$out = "$PSScriptRoot\dispatch-hypothesis-family-backfill-output.txt"
"# Hypothesis-family backfill (Phase E follow-up) - $(Get-Date -Format o)" | Out-File $out -Encoding utf8

# Clear any stale git lock so the daemon doesn't choke on us
if (Test-Path "$repo\.git\index.lock") {
    Remove-Item "$repo\.git\index.lock" -Force -ErrorAction SilentlyContinue
    "cleared stale .git/index.lock" | Add-Content $out
}

# Pre-flight: containers must be up. `docker compose ps` gives us status
# for each service. We check chili (smoke runner) and postgres (db).
$ps = & docker compose ps chili postgres 2>&1
$ps | Add-Content $out
$psStr = ($ps -join "`n")
$haveChili = $psStr -match "chili.*\s+(Up|running)\s+"
$haveDb    = $psStr -match "postgres.*\s+(Up|running)\s+"
if (-not $haveChili -or -not $haveDb) {
    "ABORT: chili and/or postgres not up (haveChili=$haveChili haveDb=$haveDb). Run dispatch-followup-activations-recreate.ps1 first, then re-queue." | Add-Content $out
    exit 1
}
"  containers verified up" | Add-Content $out

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
$auditOut = docker compose exec -T postgres psql -U chili -d chili -c $audit 2>&1
$auditOut | Add-Content $out

"=== By-family distribution (active) ===" | Add-Content $out
$dist = "SELECT COALESCE(hypothesis_family,'__NULL__') AS family, COUNT(*) FROM scan_patterns WHERE active=true GROUP BY 1 ORDER BY 2 DESC;"
docker compose exec -T postgres psql -U chili -d chili -c $dist 2>&1 | Add-Content $out

"=== Running smoke_family_backfill.py inside chili container ===" | Add-Content $out
# Copy the smoke script into the chili container and execute it
$smoke = "$repo\scripts\_smoke_family_backfill.py"
if (Test-Path $smoke) {
    # Pipe the script via stdin to avoid PowerShell argument quoting issues
    # (a previous version used `python -c $content` and PS stripped the
    # string-literal quotes inside the script, breaking sys.path.insert).
    # `-T` keeps the channel non-interactive; `-i` lets python read stdin.
    Get-Content $smoke -Raw | docker compose exec -T chili python 2>&1 | Add-Content $out
} else {
    "ERROR: smoke script not found at $smoke" | Add-Content $out
}

"=== Post-backfill coverage ===" | Add-Content $out
docker compose exec -T postgres psql -U chili -d chili -c $audit 2>&1 | Add-Content $out
docker compose exec -T postgres psql -U chili -d chili -c $dist 2>&1 | Add-Content $out

"DONE at $(Get-Date -Format o)" | Add-Content $out
