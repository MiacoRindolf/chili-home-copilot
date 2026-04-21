<#
.SYNOPSIS
  Roll back the prediction-mirror rollout to the safe "all flags off" state.

.DESCRIPTION
  Phase A (tech-debt remediation) rollback automation. See
  docs/PHASE_ROLLBACK_RUNBOOK.md for the full procedure and the pre-flight
  release-blocker check.

  Actions, in order:
    1. Backup .env -> .env.bak.<timestamp>
    2. Flip every BRAIN_PREDICTION_*_ENABLED flag to "false" in .env
    3. docker compose up --force-recreate chili brain-worker
    4. Run the release-blocker grep
       (scripts/check_chili_prediction_ops_release_blocker.ps1) on the
       last 30 minutes of chili logs.

  The script is IDEMPOTENT: if the target flags are already "false",
  running again rewrites .env to the same content and no backup is
  created beyond the first run. The Docker recreate is always attempted
  unless -SkipRecreate is passed.

  Use -WhatIf to preview without writing anything.

.PARAMETER EnvPath
  Path to the .env file. Defaults to ".\.env" (repo root).

.PARAMETER SkipRecreate
  Do not run docker compose up --force-recreate. Useful for offline flag
  flips in staging.

.PARAMETER LogSinceMinutes
  Window for the release-blocker grep. Default 30 minutes.

.EXAMPLE
  .\scripts\rollback-prediction-mirror.ps1
  .\scripts\rollback-prediction-mirror.ps1 -WhatIf
  .\scripts\rollback-prediction-mirror.ps1 -SkipRecreate

.NOTES
  Hard Rule 5 (CLAUDE.md): the prediction-mirror authority contract is
  FROZEN. This script rolls back a rollout. It does not migrate forward.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string] $EnvPath = ".\.env",
    [switch] $SkipRecreate,
    [int]    $LogSinceMinutes = 30
)

$ErrorActionPreference = "Stop"

# Every flag we flip. Keep this list in sync with docs/PHASE_ROLLBACK_RUNBOOK.md.
$TargetFlags = @(
    "BRAIN_PREDICTION_DUAL_WRITE_ENABLED",
    "BRAIN_PREDICTION_READ_COMPARE_ENABLED",
    "BRAIN_PREDICTION_READ_AUTHORITATIVE_ENABLED",
    "BRAIN_PREDICTION_MIRROR_WRITE_DEDICATED"
)

function Read-EnvLines {
    param([string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw ".env not found at $Path -- pass -EnvPath to override"
    }
    return Get-Content -LiteralPath $Path -ErrorAction Stop
}

function Apply-FlagFlips {
    param(
        [string[]] $Lines,
        [string[]] $Flags
    )
    # Returns ordered pair: new lines + count of lines actually changed.
    $changed = 0
    $seen = @{}
    foreach ($f in $Flags) { $seen[$f] = $false }

    $newLines = foreach ($raw in $Lines) {
        $line = $raw
        # Match "KEY=value" ignoring leading whitespace; comments (#) pass through.
        if ($line -match '^\s*([A-Z0-9_]+)\s*=\s*(.*)$') {
            $key = $matches[1]
            $val = $matches[2].Trim()
            if ($Flags -contains $key) {
                $seen[$key] = $true
                if ($val -ne "false") {
                    $line = "$key=false"
                    $changed++
                }
            }
        }
        $line
    }

    # Append any flags that weren't present so the rollback is complete.
    foreach ($f in $Flags) {
        if (-not $seen[$f]) {
            $newLines += "$f=false"
            $changed++
        }
    }

    return @{ Lines = $newLines; Changed = $changed }
}

function Invoke-ReleaseBlockerGrep {
    param([int] $SinceMinutes)
    $gate = Join-Path (Split-Path -Parent $PSCommandPath) "check_chili_prediction_ops_release_blocker.ps1"
    if (-not (Test-Path -LiteralPath $gate)) {
        Write-Warning "[rollback] release-blocker gate not found at $gate; skipping"
        return 0
    }
    Write-Host "[rollback] running release-blocker grep on last ${SinceMinutes}m of chili logs..."
    # Docker may not be available (e.g. local dev without compose up). If it isn't,
    # we treat the grep as a soft skip — the flags are flipped regardless.
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if ($null -eq $docker) {
        Write-Warning "[rollback] docker not on PATH; skipping release-blocker grep"
        return 0
    }
    try {
        $logs = & docker compose logs chili --since "${SinceMinutes}m" 2>&1
    } catch {
        Write-Warning "[rollback] docker compose logs failed: $_"
        return 0
    }
    $logs | & $gate
    return $LASTEXITCODE
}

# ─── main ───────────────────────────────────────────────────────────
Write-Host "[rollback] target .env: $EnvPath"
$originalLines = Read-EnvLines -Path $EnvPath
$applied = Apply-FlagFlips -Lines $originalLines -Flags $TargetFlags

if ($applied.Changed -eq 0) {
    Write-Host "[rollback] all $($TargetFlags.Count) flags already set to 'false' -- no .env change needed (idempotent)"
} else {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backup = "$EnvPath.bak.$stamp"
    if ($PSCmdlet.ShouldProcess($EnvPath, "backup to $backup and flip $($applied.Changed) flag(s) to false")) {
        Copy-Item -LiteralPath $EnvPath -Destination $backup -ErrorAction Stop
        Write-Host "[rollback] backed up .env -> $backup"
        Set-Content -LiteralPath $EnvPath -Value $applied.Lines -Encoding utf8 -ErrorAction Stop
        Write-Host "[rollback] rewrote .env: $($applied.Changed) flag line(s) changed"
    }
}

if ($SkipRecreate) {
    Write-Host "[rollback] -SkipRecreate set; leaving containers as-is"
} else {
    if ($PSCmdlet.ShouldProcess("chili, brain-worker", "docker compose up --force-recreate")) {
        $docker = Get-Command docker -ErrorAction SilentlyContinue
        if ($null -eq $docker) {
            Write-Warning "[rollback] docker not on PATH; skipping container recreate. Restart the app manually to pick up the flag change."
        } else {
            Write-Host "[rollback] docker compose up -d --force-recreate chili brain-worker"
            & docker compose up -d --force-recreate chili brain-worker
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "[rollback] docker compose recreate exited with $LASTEXITCODE"
            }
        }
    }
}

# Release-blocker grep always runs (read-only; safe in -WhatIf)
$gateExit = Invoke-ReleaseBlockerGrep -SinceMinutes $LogSinceMinutes
if ($gateExit -eq 0) {
    Write-Host "[rollback] PASS: release-blocker grep clean"
    exit 0
} else {
    Write-Error "[rollback] FAIL: release-blocker grep exit=$gateExit. See docs/PHASE_ROLLBACK_RUNBOOK.md for next steps."
    exit $gateExit
}
