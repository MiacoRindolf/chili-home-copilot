<#
.SYNOPSIS
  Fails if the /api/trading/brain/ops/health JSON dump violates the
  Phase K frozen contract.

.DESCRIPTION
  This script is a JSON-only gate. Unlike the per-phase release
  blockers, the ops-health endpoint is read-only and has no
  authoritative-mode risk; instead we gate on wire-shape stability
  and operational severity.

  Pass the path to a JSON dump via -DiagnosticsJson. The gate fails
  when:
    * Required top-level keys are missing (ok, ops_health)
    * Required ops_health keys are missing (overall_severity,
      lookback_days, scheduler, governance, phases)
    * Expected phase keys are missing from the phases list
    * Optional ``-FailOnRedOverall`` is passed and
      ``overall_severity == "red"``

  Exit 0 = contract intact (and optional severity gate clean).
  Exit 1 = contract violation (missing keys) or severity gate fail.
  Exit 2 = file not found.
  Exit 3 = malformed JSON.

.EXAMPLE
  curl -sk https://localhost:8000/api/trading/brain/ops/health -o oh.json
  .\scripts\check_ops_health_release_blocker.ps1 -DiagnosticsJson .\oh.json

.EXAMPLE
  .\scripts\check_ops_health_release_blocker.ps1 -DiagnosticsJson .\oh.json -FailOnRedOverall
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $DiagnosticsJson,
    [Parameter()]
    [switch] $FailOnRedOverall
)

$ExpectedPhaseKeys = @(
    "ledger", "exit_engine", "net_edge", "pit", "triple_barrier",
    "execution_cost", "venue_truth", "bracket_intent",
    "bracket_reconciliation", "position_sizer", "risk_dial",
    "capital_reweight", "drift_monitor", "recert_queue", "divergence"
)

if (-not (Test-Path -LiteralPath $DiagnosticsJson)) {
    Write-Error "File not found: $DiagnosticsJson"
    exit 2
}

try {
    $payload = Get-Content -Raw -LiteralPath $DiagnosticsJson | ConvertFrom-Json
} catch {
    Write-Error "Malformed JSON in $DiagnosticsJson : $($_.Exception.Message)"
    exit 3
}

# Top-level contract
if ($null -eq $payload.ok) {
    Write-Error "Release blocker: missing top-level key 'ok'"
    exit 1
}
if ($null -eq $payload.ops_health) {
    Write-Error "Release blocker: missing top-level key 'ops_health'"
    exit 1
}

$oh = $payload.ops_health

$RequiredOpsHealthKeys = @(
    "overall_severity", "lookback_days", "scheduler",
    "governance", "phases"
)
foreach ($k in $RequiredOpsHealthKeys) {
    if (-not ($oh.PSObject.Properties.Name -contains $k)) {
        Write-Error "Release blocker: ops_health missing required key '$k'"
        exit 1
    }
}

if ($null -eq $oh.scheduler.running -or $null -eq $oh.scheduler.job_count) {
    Write-Error "Release blocker: ops_health.scheduler missing {running, job_count}"
    exit 1
}

if (
    $null -eq $oh.governance.kill_switch_engaged `
    -or $null -eq $oh.governance.pending_approvals
) {
    Write-Error "Release blocker: ops_health.governance missing {kill_switch_engaged, pending_approvals}"
    exit 1
}

$phaseKeys = @()
foreach ($p in $oh.phases) {
    $phaseKeys += [string]$p.key
}
foreach ($k in $ExpectedPhaseKeys) {
    if ($phaseKeys -notcontains $k) {
        Write-Error "Release blocker: ops_health.phases missing expected phase '$k'"
        exit 1
    }
}

if ($FailOnRedOverall -and [string]$oh.overall_severity -eq "red") {
    Write-Error "Release blocker: overall_severity=red (FailOnRedOverall enabled)"
    exit 1
}

exit 0
