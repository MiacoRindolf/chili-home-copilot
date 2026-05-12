# Phase 3 of f-adaptive-promotion-architecture — one-shot backfill of
# scan_patterns.quality_composite_score. Reuses the streaming wrapper
# `compute_and_persist_scores_streaming` from
# app/services/trading/pattern_quality_score.py.
#
# Contract:
#   - -DryRun defaults to $true. Live runs require explicit
#     -DryRun:$false. Dry-run mode loads + computes + emits the would-
#     write distribution but rolls back per batch (no UPDATE commits).
#   - Kill switch via scripts/quality-score-backfill-stop.flag — touched
#     by the operator while the script is running. The Python streaming
#     wrapper checks the flag between batches and exits cleanly with
#     ``stopped_by_flag=true`` in the result dict.
#   - Per-pattern progress is appended to
#     scripts/quality-score-backfill-progress.log when -Verbose is set.
#   - The script invokes Python inside the chili container via
#     ``docker compose exec -T chili``. The host doesn't need its own
#     chili-env; the container is canonical.

[CmdletBinding()]
param(
    [int]$BatchSize = 50,
    [bool]$DryRun = $true,
    [switch]$VerboseProgress
)

$ErrorActionPreference = 'Stop'

$STOP_FLAG = Join-Path $PSScriptRoot 'quality-score-backfill-stop.flag'
$PROGRESS_LOG = Join-Path $PSScriptRoot 'quality-score-backfill-progress.log'
$CONTAINER_STOP_FLAG = "/app/scripts/quality-score-backfill-stop.flag"

if ($BatchSize -lt 1) {
    Write-Host "ERROR: -BatchSize must be >= 1 (got $BatchSize)" -ForegroundColor Red
    exit 2
}

Write-Host ""
Write-Host "=== quality-score-backfill (Phase 3) ===" -ForegroundColor Cyan
Write-Host "BatchSize:        $BatchSize"
Write-Host "DryRun:           $DryRun"
Write-Host "VerboseProgress:  $($VerboseProgress.IsPresent)"
Write-Host "StopFlag (host):  $STOP_FLAG"
Write-Host "StopFlag (cont):  $CONTAINER_STOP_FLAG"
Write-Host "ProgressLog:      $PROGRESS_LOG"
Write-Host ""
Write-Host "Reuses: pattern_quality_score.compute_and_persist_scores_streaming"
Write-Host ""

if (Test-Path $STOP_FLAG) {
    if (-not $DryRun) {
        Write-Host "ERROR: stop flag present at $STOP_FLAG. Remove before live run." -ForegroundColor Red
        exit 3
    }
    Write-Host "NOTE: stop flag is present; -DryRun will short-circuit after first batch." -ForegroundColor Yellow
}

$dryRunPy = if ($DryRun) { 'True' } else { 'False' }
$verbosePy = if ($VerboseProgress.IsPresent) { 'True' } else { 'False' }

$pyScript = @"
from __future__ import annotations

import json
import sys
from app.db import SessionLocal
from app.services.trading.pattern_quality_score import (
    compute_and_persist_scores_streaming,
)


def _emit(record: dict) -> None:
    # PowerShell parses per-line. Keep one JSON record per stdout line
    # prefixed with ``PROGRESS `` so the host script can grep for it
    # and separate from logger output.
    try:
        print('PROGRESS ' + json.dumps(record, default=str), flush=True)
    except Exception:
        pass


def main() -> int:
    sess = SessionLocal()
    try:
        result = compute_and_persist_scores_streaming(
            sess,
            batch_size=$BatchSize,
            stop_flag_path='$CONTAINER_STOP_FLAG',
            dry_run=$dryRunPy,
            on_pattern=(_emit if $verbosePy else None),
        )
        print('RESULT ' + json.dumps(result, default=str), flush=True)
        return 0
    except Exception as exc:
        print('ERROR ' + repr(exc), file=sys.stderr, flush=True)
        try:
            sess.rollback()
        except Exception:
            pass
        return 1
    finally:
        try:
            sess.close()
        except Exception:
            pass


sys.exit(main())
"@

$runStart = Get-Date
$startLine = "{0:o}`tSTART`tdry_run={1}`tbatch_size={2}`tverbose={3}" -f $runStart, $DryRun, $BatchSize, $VerboseProgress.IsPresent
Add-Content -Path $PROGRESS_LOG -Value $startLine -Encoding utf8

Write-Host "[1/3] Running backfill in chili container..."
$tempScriptHost = New-TemporaryFile
try {
    Set-Content -Path $tempScriptHost -Value $pyScript -Encoding utf8
    $containerTmp = "/tmp/quality_score_backfill_$([System.Guid]::NewGuid().ToString('N').Substring(0,8)).py"

    # Copy script into the container, then exec python.
    Get-Content $tempScriptHost | & docker compose exec -T chili tee $containerTmp > $null
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose exec failed staging script (exit=$LASTEXITCODE)"
    }

    $cmdline = "cd /app && PYTHONPATH=/app python $containerTmp"
    Write-Host "  -> $cmdline"

    & docker compose exec -T chili bash -lc $cmdline 2>&1 | ForEach-Object {
        $line = "$_"
        if ($line.StartsWith('PROGRESS ')) {
            $payload = $line.Substring(9).Trim()
            $logLine = "{0:o}`tPATTERN`t{1}" -f (Get-Date), $payload
            Add-Content -Path $PROGRESS_LOG -Value $logLine -Encoding utf8
            if ($VerboseProgress.IsPresent) {
                Write-Host "  $payload"
            }
        } elseif ($line.StartsWith('RESULT ')) {
            $payload = $line.Substring(7).Trim()
            $logLine = "{0:o}`tRESULT`t{1}" -f (Get-Date), $payload
            Add-Content -Path $PROGRESS_LOG -Value $logLine -Encoding utf8
            Write-Host ""
            Write-Host "[2/3] Result:" -ForegroundColor Cyan
            Write-Host "  $payload"
        } else {
            Write-Host $line
        }
    }

    if ($LASTEXITCODE -ne 0) {
        $errLine = "{0:o}`tERROR`texit_code={1}" -f (Get-Date), $LASTEXITCODE
        Add-Content -Path $PROGRESS_LOG -Value $errLine -Encoding utf8
        throw "Python backfill failed (exit=$LASTEXITCODE). See above + $PROGRESS_LOG."
    }

    # Clean up the staged script inside the container.
    & docker compose exec -T chili rm -f $containerTmp 2>$null | Out-Null
}
finally {
    Remove-Item -Path $tempScriptHost -Force -ErrorAction SilentlyContinue
}

$elapsed = (Get-Date) - $runStart
$doneLine = "{0:o}`tDONE`tdry_run={1}`telapsed_sec={2:N1}" -f (Get-Date), $DryRun, $elapsed.TotalSeconds
Add-Content -Path $PROGRESS_LOG -Value $doneLine -Encoding utf8

Write-Host ""
Write-Host "[3/3] Finished in $([Math]::Round($elapsed.TotalSeconds,1))s." -ForegroundColor Green
Write-Host "Progress log: $PROGRESS_LOG"
if ($DryRun) {
    Write-Host ""
    Write-Host "DRY-RUN: no UPDATE commits. Re-run with -DryRun:`$false to apply." -ForegroundColor Yellow
}
exit 0
