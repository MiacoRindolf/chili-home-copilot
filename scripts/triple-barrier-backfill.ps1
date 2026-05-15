# Phase C of f-evidence-fidelity-architecture (2026-05-14) -- one-shot
# backfill that primes ``trading_triple_barrier_labels`` against older
# MarketSnapshots in batches. The 4-hourly cron only touches the most
# recent 500 snapshots per cycle; this script walks the full backlog so
# the table goes from "0 rows" to "useful for meta-classifier training"
# in a single operator-supervised pass.
#
# Contract:
#   - -DryRun defaults to $true. Live runs require explicit
#     -DryRun:$false. Dry-run forces ``mode_override='off'`` so the
#     labeler computes but does not insert; report fields still come
#     back so the operator can size the run.
#   - Kill switch via scripts/triple-barrier-backfill-stop.flag --
#     touched by the operator while the script is running. The Python
#     side checks the flag between batches and exits cleanly with
#     ``stopped_by_flag=true`` in the result dict.
#   - The labeler itself (label_snapshots) is idempotent via
#     uq_triple_barrier_labels: re-running a batch that overlaps a prior
#     run just bumps skipped_existing.
#   - The labeler walks newest-first per call. To reach older
#     snapshots, this script raises ``min_lookback_days`` across passes:
#     1 -> 14, 30, 60, 90, 180. Each pass labels up to ``-BatchSize``
#     snapshots whose snapshot_date is at least that many days old.
#     Larger lookback windows reach deeper into history. We continue
#     until written + skipped_existing < ``-BatchSize`` for the pass
#     (i.e. labeler ran out of unlabeled rows to consume) or
#     ``-MaxPasses`` is hit.
#   - Per-pass progress logged to scripts/triple-barrier-backfill-progress.log.
#   - Runs inside the chili container; the host doesn't need its own
#     chili-env.

[CmdletBinding()]
param(
    [int]$BatchSize = 500,
    [int]$MaxPasses = 12,
    [int[]]$LookbackDays = @(14, 30, 60, 90, 180, 365),
    [bool]$DryRun = $true,
    [switch]$VerboseProgress
)

$ErrorActionPreference = 'Stop'

$STOP_FLAG = Join-Path $PSScriptRoot 'triple-barrier-backfill-stop.flag'
$PROGRESS_LOG = Join-Path $PSScriptRoot 'triple-barrier-backfill-progress.log'
$CONTAINER_STOP_FLAG = "/app/scripts/triple-barrier-backfill-stop.flag"

if ($BatchSize -lt 1) {
    Write-Host "ERROR: -BatchSize must be >= 1 (got $BatchSize)" -ForegroundColor Red
    exit 2
}
if ($MaxPasses -lt 1) {
    Write-Host "ERROR: -MaxPasses must be >= 1 (got $MaxPasses)" -ForegroundColor Red
    exit 2
}
if ($null -eq $LookbackDays -or $LookbackDays.Count -lt 1) {
    Write-Host "ERROR: -LookbackDays must contain at least one positive integer" -ForegroundColor Red
    exit 2
}
foreach ($d in $LookbackDays) {
    if ($d -lt 1) {
        Write-Host "ERROR: -LookbackDays entries must be >= 1 (got $d)" -ForegroundColor Red
        exit 2
    }
}

Write-Host ""
Write-Host "=== triple-barrier-backfill (Phase C) ===" -ForegroundColor Cyan
Write-Host "BatchSize:        $BatchSize"
Write-Host "MaxPasses:        $MaxPasses"
Write-Host "LookbackDays:     $($LookbackDays -join ', ')"
Write-Host "DryRun:           $DryRun"
Write-Host "VerboseProgress:  $($VerboseProgress.IsPresent)"
Write-Host "StopFlag (host):  $STOP_FLAG"
Write-Host "StopFlag (cont):  $CONTAINER_STOP_FLAG"
Write-Host "ProgressLog:      $PROGRESS_LOG"
Write-Host ""
Write-Host "Mode override:    $(if ($DryRun) { 'off (dry-run: compute only, no inserts)' } else { 'shadow (writes labels but no downstream gate effect)' })"
Write-Host ""

if (Test-Path $STOP_FLAG) {
    if (-not $DryRun) {
        Write-Host "ERROR: stop flag present at $STOP_FLAG. Remove before live run." -ForegroundColor Red
        exit 3
    }
    Write-Host "NOTE: stop flag is present; -DryRun will short-circuit before first batch." -ForegroundColor Yellow
}

$dryRunPy = if ($DryRun) { 'True' } else { 'False' }
$verbosePy = if ($VerboseProgress.IsPresent) { 'True' } else { 'False' }
$lookbackPy = '[' + (($LookbackDays | ForEach-Object { [int]$_ }) -join ', ') + ']'

$pyScript = @"
from __future__ import annotations

import json
import os
import sys

from app.db import SessionLocal
from app.services.trading.triple_barrier_labeler import label_snapshots


STOP_FLAG = '$CONTAINER_STOP_FLAG'
DRY_RUN = $dryRunPy
BATCH_SIZE = $BatchSize
MAX_PASSES = $MaxPasses
LOOKBACK_DAYS_LIST = $lookbackPy
VERBOSE = $verbosePy
MODE_OVERRIDE = 'off' if DRY_RUN else 'shadow'


def _emit(kind: str, record: dict) -> None:
    try:
        print('PROGRESS ' + json.dumps({'kind': kind, **record}, default=str), flush=True)
    except Exception:
        pass


def _stop_flag_present() -> bool:
    try:
        return os.path.exists(STOP_FLAG)
    except Exception:
        return False


def _run_pass(sess, lookback_days: int, pass_idx: int) -> dict:
    """Single labeler call. Returns the dict-form of LabelerReport
    plus a stopped_by_flag bool for the caller.
    """
    if _stop_flag_present():
        return {
            'stopped_by_flag': True,
            'lookback_days': lookback_days,
            'requested': 0,
            'written': 0,
            'skipped_existing': 0,
            'missing_data': 0,
            'errors': 0,
        }

    rep = label_snapshots(
        sess,
        limit=BATCH_SIZE,
        side='long',
        min_lookback_days=lookback_days,
        mode_override=MODE_OVERRIDE,
    )
    summary = {
        'pass_idx': pass_idx,
        'lookback_days': lookback_days,
        'mode': rep.mode,
        'requested': rep.requested,
        'written': rep.written,
        'skipped_existing': rep.skipped_existing,
        'missing_data': rep.missing_data,
        'labels_tp': rep.labels_tp,
        'labels_sl': rep.labels_sl,
        'labels_timeout': rep.labels_timeout,
        'errors': rep.errors,
        'stopped_by_flag': False,
    }
    if VERBOSE:
        _emit('pass.done', summary)
    return summary


def main() -> int:
    sess = SessionLocal()
    try:
        _emit('start', {
            'dry_run': DRY_RUN,
            'batch_size': BATCH_SIZE,
            'max_passes': MAX_PASSES,
            'lookback_days_list': LOOKBACK_DAYS_LIST,
            'mode_override': MODE_OVERRIDE,
        })

        all_passes = []
        stopped = False
        pass_idx = 0
        for lookback in LOOKBACK_DAYS_LIST:
            # Repeat at the same lookback until labeler runs dry (the
            # newest-first scan keeps eating the head of the queue for
            # that lookback until requested < BATCH_SIZE).
            while True:
                if pass_idx >= MAX_PASSES:
                    _emit('cap_hit', {'max_passes': MAX_PASSES})
                    break
                pass_idx += 1
                summary = _run_pass(sess, lookback_days=lookback, pass_idx=pass_idx)
                all_passes.append(summary)
                if summary['stopped_by_flag']:
                    stopped = True
                    break
                processed = summary['written'] + summary['skipped_existing'] + summary['missing_data']
                if summary['requested'] < BATCH_SIZE:
                    # labeler ran out of candidate snapshots at this lookback
                    break
                if processed == 0:
                    # safety: nothing happened, don't loop forever
                    break
            if stopped or pass_idx >= MAX_PASSES:
                break

        totals = {
            'passes': len(all_passes),
            'total_written': sum(p['written'] for p in all_passes),
            'total_skipped_existing': sum(p['skipped_existing'] for p in all_passes),
            'total_missing_data': sum(p['missing_data'] for p in all_passes),
            'total_errors': sum(p['errors'] for p in all_passes),
            'stopped_by_flag': stopped,
            'cap_hit': pass_idx >= MAX_PASSES and not stopped,
        }
        _emit('totals', totals)
        print('RESULT ' + json.dumps({'passes': all_passes, 'totals': totals}, default=str), flush=True)
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
$startLine = "{0:o}`tSTART`tdry_run={1}`tbatch_size={2}`tmax_passes={3}`tlookback={4}`tverbose={5}" -f $runStart, $DryRun, $BatchSize, $MaxPasses, ($LookbackDays -join ','), $VerboseProgress.IsPresent
Add-Content -Path $PROGRESS_LOG -Value $startLine -Encoding utf8

Write-Host "[1/3] Running backfill in chili container..."
$tempScriptHost = New-TemporaryFile
try {
    Set-Content -Path $tempScriptHost -Value $pyScript -Encoding utf8
    $containerTmp = "/tmp/triple_barrier_backfill_$([System.Guid]::NewGuid().ToString('N').Substring(0,8)).py"

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
            $logLine = "{0:o}`tPROGRESS`t{1}" -f (Get-Date), $payload
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
    Write-Host "DRY-RUN: mode_override='off' forced -- no labels written. Re-run with -DryRun:`$false to apply." -ForegroundColor Yellow
}
exit 0
