# Phase B of f-evidence-fidelity-architecture (2026-05-14) -- historical
# backfill of ``trading_venue_truth_log`` from closed Trade rows + their
# TradingExecutionEvent rows. Tables already exist (mig 196/197); they
# have just never been populated. This script walks the past N days of
# closed Trades and emits one venue_truth_log row per trade.
#
# Contract:
#   - -DryRun defaults to $true. Live runs require explicit
#     -DryRun:$false. Dry-run computes + logs a per-broker summary but
#     does NOT write to trading_venue_truth_log.
#   - Kill switch via scripts/venue-truth-backfill-stop.flag --
#     touched by the operator while the script is running. The Python
#     side checks the flag between batches and exits cleanly with
#     ``stopped_by_flag=true`` in the result dict.
#   - Idempotent. trading_venue_truth_log has NO unique constraint on
#     trade_id, so the script pre-queries the set of already-recorded
#     trade_ids and skips them. Safe to re-run.
#   - record_fill_observation mode stays "shadow" -- writes happen but
#     no live trading consumes them yet.
#   - Runs inside the chili container; the host doesn't need its own
#     chili-env.
#
# Usage:
#   .\scripts\venue-truth-backfill.ps1                       # dry-run, 30d, default
#   .\scripts\venue-truth-backfill.ps1 -DryRun:$false         # live write
#   .\scripts\venue-truth-backfill.ps1 -LookbackDays 7        # last week only
#   .\scripts\venue-truth-backfill.ps1 -DryRun:$false -BatchSize 50

[CmdletBinding()]
param(
    [int]$LookbackDays = 30,
    [int]$BatchSize = 100,
    [bool]$DryRun = $true,
    [switch]$VerboseProgress
)

$ErrorActionPreference = 'Stop'

$STOP_FLAG = Join-Path $PSScriptRoot 'venue-truth-backfill-stop.flag'
$PROGRESS_LOG = Join-Path $PSScriptRoot 'venue-truth-backfill-progress.log'
$CONTAINER_STOP_FLAG = "/app/scripts/venue-truth-backfill-stop.flag"

if ($BatchSize -lt 1) {
    Write-Host "ERROR: -BatchSize must be >= 1 (got $BatchSize)" -ForegroundColor Red
    exit 2
}
if ($LookbackDays -lt 1) {
    Write-Host "ERROR: -LookbackDays must be >= 1 (got $LookbackDays)" -ForegroundColor Red
    exit 2
}

Write-Host ""
Write-Host "=== venue-truth-backfill (Phase B) ===" -ForegroundColor Cyan
Write-Host "LookbackDays:     $LookbackDays"
Write-Host "BatchSize:        $BatchSize"
Write-Host "DryRun:           $DryRun"
Write-Host "VerboseProgress:  $($VerboseProgress.IsPresent)"
Write-Host "StopFlag (host):  $STOP_FLAG"
Write-Host "StopFlag (cont):  $CONTAINER_STOP_FLAG"
Write-Host "ProgressLog:      $PROGRESS_LOG"
Write-Host ""
Write-Host "Walks closed Trades in the past $LookbackDays days; emits one"
Write-Host "venue_truth_log row per trade in shadow mode."
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
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import text

from app.db import SessionLocal
from app.models.trading import Trade
from app.services.trading.brain_work.execution_hooks import (
    _compute_fill_observation,
)
from app.services.trading.venue_truth import record_fill_observation


STOP_FLAG = '$CONTAINER_STOP_FLAG'
DRY_RUN = $dryRunPy
BATCH_SIZE = $BatchSize
LOOKBACK_DAYS = $LookbackDays
VERBOSE = $verbosePy


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


def _existing_trade_ids(sess) -> set:
    rows = sess.execute(text(
        'SELECT DISTINCT trade_id FROM trading_venue_truth_log WHERE trade_id IS NOT NULL'
    )).fetchall()
    return {int(r[0]) for r in rows if r[0] is not None}


def _candidate_trades(sess, *, lookback_days: int):
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    return (
        sess.query(Trade)
        .filter(
            Trade.status == 'closed',
            Trade.exit_date.isnot(None),
            Trade.exit_date >= cutoff,
        )
        .order_by(Trade.exit_date.asc())
        .all()
    )


def main() -> int:
    sess = SessionLocal()
    try:
        _emit('start', {
            'dry_run': DRY_RUN,
            'batch_size': BATCH_SIZE,
            'lookback_days': LOOKBACK_DAYS,
        })

        already = _existing_trade_ids(sess)
        _emit('already_recorded', {'n': len(already)})

        trades = _candidate_trades(sess, lookback_days=LOOKBACK_DAYS)
        _emit('candidates_loaded', {'n': len(trades)})

        per_broker = defaultdict(lambda: {
            'count': 0,
            'realized_cost_fraction_sum': 0.0,
            'realized_cost_fraction_n': 0,
            'cost_gap_bps_sum': 0.0,
            'cost_gap_bps_n': 0,
        })

        written = 0
        skipped_already = 0
        skipped_no_obs = 0
        stopped = False
        batch_n = 0
        for t in trades:
            if _stop_flag_present():
                stopped = True
                break
            tid = int(t.id)
            if tid in already:
                skipped_already += 1
                continue
            obs = _compute_fill_observation(sess, t, paper_bool=False)
            if obs is None:
                skipped_no_obs += 1
                continue

            broker = (t.broker_source or 'manual').strip().lower()
            stats = per_broker[broker]
            stats['count'] += 1
            if obs.realized_cost_fraction is not None:
                stats['realized_cost_fraction_sum'] += float(obs.realized_cost_fraction)
                stats['realized_cost_fraction_n'] += 1
            if (
                obs.expected_cost_fraction is not None
                and obs.realized_cost_fraction is not None
            ):
                gap_bps = (
                    float(obs.realized_cost_fraction)
                    - float(obs.expected_cost_fraction)
                ) * 10_000.0
                stats['cost_gap_bps_sum'] += gap_bps
                stats['cost_gap_bps_n'] += 1

            if VERBOSE:
                _emit('row', {
                    'trade_id': tid,
                    'ticker': t.ticker,
                    'broker': broker,
                    'realized_cost_fraction': obs.realized_cost_fraction,
                    'expected_cost_fraction': obs.expected_cost_fraction,
                })

            if not DRY_RUN:
                record_fill_observation(sess, obs)
                written += 1

            batch_n += 1
            if batch_n >= BATCH_SIZE:
                batch_n = 0
                # record_fill_observation commits internally per row in
                # live mode; nothing to do here in dry-run.
                _emit('batch', {'written_so_far': written})

        # Per-broker summary
        summary = {}
        for broker, stats in per_broker.items():
            n_real = stats['realized_cost_fraction_n']
            n_gap = stats['cost_gap_bps_n']
            summary[broker] = {
                'count': stats['count'],
                'mean_realized_cost_fraction': (
                    stats['realized_cost_fraction_sum'] / n_real
                ) if n_real > 0 else None,
                'mean_cost_gap_bps': (
                    stats['cost_gap_bps_sum'] / n_gap
                ) if n_gap > 0 else None,
            }
        _emit('summary', summary)

        result = {
            'dry_run': DRY_RUN,
            'candidates': len(trades),
            'already_recorded': len(already),
            'written': written,
            'skipped_already': skipped_already,
            'skipped_no_obs': skipped_no_obs,
            'stopped_by_flag': stopped,
            'per_broker': summary,
        }
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
$startLine = "{0:o}`tSTART`tdry_run={1}`tbatch_size={2}`tlookback_days={3}`tverbose={4}" -f $runStart, $DryRun, $BatchSize, $LookbackDays, $VerboseProgress.IsPresent
Add-Content -Path $PROGRESS_LOG -Value $startLine -Encoding utf8

Write-Host "[1/3] Running backfill in chili container..."
$tempScriptHost = New-TemporaryFile
try {
    Set-Content -Path $tempScriptHost -Value $pyScript -Encoding utf8
    $containerTmp = "/tmp/venue_truth_backfill_$([System.Guid]::NewGuid().ToString('N').Substring(0,8)).py"

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
