# Phase A of f-evidence-fidelity-architecture (2026-05-14) -- one-shot
# backfill that primes ``scan_patterns.corrected_*`` and ``raw_realized_*``
# from existing pre-PR data. Migration 241 adds the columns; this script
# fills them so readers don't see NULL during the merge window.
#
# Contract:
#   - -DryRun defaults to $true. Live runs require explicit
#     -DryRun:$false. Dry-run mode loads + computes + emits per-pattern
#     deltas but rolls back (no UPDATE commits).
#   - Kill switch via scripts/canonical-outcome-backfill-stop.flag --
#     touched by the operator while the script is running. The Python
#     side checks the flag between batches and exits cleanly with
#     ``stopped_by_flag=true`` in the result dict.
#   - Idempotent. Pass A overwrites corrected_* from current legacy
#     columns (legacy = corrected post-PR in steady state; safe to re-
#     run). Pass B reuses sync_realized_stats (it writes ONLY
#     raw_realized_* post-PR).
#   - Two passes:
#       Pass A (corrected_*): for each active pattern with at least
#         one of {trade_count, win_rate, avg_return_pct} populated,
#         copy those into corrected_* and stamp
#         corrected_stats_updated_at. Mirrors the writer contract in
#         learning._evidence_correction_persist.
#       Pass B (raw_realized_*): calls sync_realized_stats(...) which
#         now writes only raw_realized_*.
#   - Logs a >20% / >50% raw-vs-corrected divergence histogram from
#     the post-backfill rows so the operator can see the magnitude
#     before flipping any reader cutover.
#   - Runs inside the chili container; the host doesn't need its own
#     chili-env.

[CmdletBinding()]
param(
    [int]$BatchSize = 100,
    [bool]$DryRun = $true,
    [switch]$VerboseProgress
)

$ErrorActionPreference = 'Stop'

$STOP_FLAG = Join-Path $PSScriptRoot 'canonical-outcome-backfill-stop.flag'
$PROGRESS_LOG = Join-Path $PSScriptRoot 'canonical-outcome-backfill-progress.log'
$CONTAINER_STOP_FLAG = "/app/scripts/canonical-outcome-backfill-stop.flag"

if ($BatchSize -lt 1) {
    Write-Host "ERROR: -BatchSize must be >= 1 (got $BatchSize)" -ForegroundColor Red
    exit 2
}

Write-Host ""
Write-Host "=== canonical-outcome-backfill (Phase A) ===" -ForegroundColor Cyan
Write-Host "BatchSize:        $BatchSize"
Write-Host "DryRun:           $DryRun"
Write-Host "VerboseProgress:  $($VerboseProgress.IsPresent)"
Write-Host "StopFlag (host):  $STOP_FLAG"
Write-Host "StopFlag (cont):  $CONTAINER_STOP_FLAG"
Write-Host "ProgressLog:      $PROGRESS_LOG"
Write-Host ""
Write-Host "Pass A: prime corrected_* from current legacy columns."
Write-Host "Pass B: refresh raw_realized_* via sync_realized_stats."
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
import math
import os
import sys
from datetime import datetime

from sqlalchemy import text

from app.db import SessionLocal
from app.services.trading.realized_stats_sync import sync_realized_stats


STOP_FLAG = '$CONTAINER_STOP_FLAG'
DRY_RUN = $dryRunPy
BATCH_SIZE = $BatchSize
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


def _pass_a_prime_corrected(sess) -> dict:
    """Copy legacy {trade_count, win_rate, avg_return_pct} into
    corrected_* for every pattern that has at least one non-null
    legacy value AND corrected_* is currently NULL on all three.
    Idempotent: re-running is a no-op once corrected_* is set.
    """
    rows = sess.execute(text('''
        SELECT id, trade_count, win_rate, avg_return_pct,
               corrected_trade_count, corrected_win_rate,
               corrected_avg_return_pct
        FROM scan_patterns
        WHERE (trade_count IS NOT NULL
               OR win_rate IS NOT NULL
               OR avg_return_pct IS NOT NULL)
        ORDER BY id
    ''')).fetchall()

    primed = 0
    skipped_already = 0
    skipped_invalid = 0
    stopped = False
    batch_n = 0
    now = datetime.utcnow()
    for r in rows:
        if _stop_flag_present():
            stopped = True
            break
        pid = int(r[0])
        n_leg = r[1]
        wr_leg = r[2]
        ret_leg = r[3]
        n_cor = r[4]
        wr_cor = r[5]
        ret_cor = r[6]
        if n_cor is not None and wr_cor is not None and ret_cor is not None:
            skipped_already += 1
            continue
        # Range / NaN safety -- migration 241 mirrored migration 193's
        # CHECK(win_rate ∈ [0,1]) onto corrected_win_rate.
        wr_ok = (wr_leg is None) or (
            isinstance(wr_leg, (int, float))
            and math.isfinite(float(wr_leg))
            and 0.0 <= float(wr_leg) <= 1.0
        )
        ret_ok = (ret_leg is None) or (
            isinstance(ret_leg, (int, float))
            and math.isfinite(float(ret_leg))
        )
        if not (wr_ok and ret_ok):
            skipped_invalid += 1
            continue

        if VERBOSE:
            _emit('passA.row', {
                'pid': pid,
                'legacy_n': n_leg,
                'legacy_wr': wr_leg,
                'legacy_ret': ret_leg,
            })

        if not DRY_RUN:
            sess.execute(text('''
                UPDATE scan_patterns
                SET corrected_trade_count = COALESCE(:n, corrected_trade_count),
                    corrected_win_rate = COALESCE(:wr, corrected_win_rate),
                    corrected_avg_return_pct = COALESCE(:ret, corrected_avg_return_pct),
                    corrected_stats_updated_at = :ts
                WHERE id = :pid
            '''), {
                'pid': pid,
                'n': int(n_leg) if n_leg is not None else None,
                'wr': float(wr_leg) if wr_leg is not None else None,
                'ret': float(ret_leg) if ret_leg is not None else None,
                'ts': now,
            })
        primed += 1
        batch_n += 1
        if batch_n >= BATCH_SIZE:
            if not DRY_RUN:
                sess.commit()
            batch_n = 0

    if batch_n > 0 and not DRY_RUN:
        sess.commit()

    if DRY_RUN:
        sess.rollback()

    return {
        'primed': primed,
        'skipped_already': skipped_already,
        'skipped_invalid': skipped_invalid,
        'stopped_by_flag': stopped,
    }


def _divergence_histogram(sess) -> dict:
    """After Pass A + Pass B, summarise abs(raw - corrected) / corrected
    across patterns where both columns are non-null. Counts only --
    no rows updated."""
    row = sess.execute(text('''
        WITH paired AS (
            SELECT
                id,
                corrected_win_rate AS cw,
                raw_realized_win_rate AS rw
            FROM scan_patterns
            WHERE corrected_win_rate IS NOT NULL
              AND raw_realized_win_rate IS NOT NULL
        ),
        deltas AS (
            SELECT
                id,
                CASE
                    WHEN ABS(cw) > 0
                    THEN ABS(rw - cw) / ABS(cw)
                    ELSE NULL
                END AS delta
            FROM paired
        )
        SELECT
            COUNT(*) AS n_paired,
            SUM(CASE WHEN delta >= 0.50 THEN 1 ELSE 0 END) AS n_warn,
            SUM(CASE WHEN delta >= 0.20 AND delta < 0.50 THEN 1 ELSE 0 END) AS n_info,
            SUM(CASE WHEN delta < 0.20 THEN 1 ELSE 0 END) AS n_quiet
        FROM deltas
    ''')).first()
    if row is None:
        return {'n_paired': 0, 'n_warn': 0, 'n_info': 0, 'n_quiet': 0}
    return {
        'n_paired': int(row[0] or 0),
        'n_warn': int(row[1] or 0),
        'n_info': int(row[2] or 0),
        'n_quiet': int(row[3] or 0),
    }


def main() -> int:
    sess = SessionLocal()
    try:
        _emit('start', {'dry_run': DRY_RUN, 'batch_size': BATCH_SIZE})

        pass_a = _pass_a_prime_corrected(sess)
        _emit('passA.done', pass_a)
        if pass_a.get('stopped_by_flag'):
            print('RESULT ' + json.dumps({'pass_a': pass_a, 'pass_b': None, 'histogram': None}, default=str), flush=True)
            return 0

        pass_b = sync_realized_stats(sess, dry_run=DRY_RUN)
        _emit('passB.done', pass_b)

        hist = _divergence_histogram(sess)
        _emit('histogram', hist)

        result = {'pass_a': pass_a, 'pass_b': pass_b, 'histogram': hist}
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
    $containerTmp = "/tmp/canonical_outcome_backfill_$([System.Guid]::NewGuid().ToString('N').Substring(0,8)).py"

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
    Write-Host "DRY-RUN: no UPDATE commits. Re-run with -DryRun:`$false to apply." -ForegroundColor Yellow
}
exit 0
