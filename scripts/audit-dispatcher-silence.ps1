# scripts/audit-dispatcher-silence.ps1
#
# Read-only audit (Phase 1a of f-adaptive-promotion-architecture).
# Tests six hypotheses about why the brain_work dispatcher loop appears
# silent and identifies which writer is marking ``backtest_completed``
# events as ``done`` without invoking the cpcv_gate handler.
#
# Constraints (do not relax in this script):
#   * READ-ONLY. No DB writes. No ``app/`` code changes. No restarts.
#   * psql -c SELECT-only.
#   * Any docker exec python -c block must rollback() in finally.
#
# Author: Claude Code, 2026-05-11.
# Brief: docs/STRATEGY/QUEUED/f-cpcv-gate-dispatcher-silence-audit.md

$ErrorActionPreference = 'Continue'

$OutFile = Join-Path $PSScriptRoot 'audit-dispatcher-silence-out.txt'
if (Test-Path $OutFile) { Remove-Item $OutFile -Force }

function Emit($line) {
    Add-Content -Path $OutFile -Value $line -Encoding utf8
    Write-Host $line
}

function Section($title) {
    Emit ''
    Emit ('=' * 72)
    Emit $title
    Emit ('=' * 72)
}

Emit "audit-dispatcher-silence  $(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')"
Emit "host=$env:COMPUTERNAME  user=$env:USERNAME"

# ---------------------------------------------------------------- inputs

$BrainWorker = 'chili-home-copilot-brain-worker-1'
$Postgres    = 'chili-home-copilot-postgres-1'
$AllWorkers  = @(
    'brain-worker', 'scheduler-worker', 'autotrader-worker',
    'broker-sync-worker', 'fast-data-worker', 'chili'
)
$HandlerPrefixes = @(
    'brain_work_dispatch',
    'brain_work:cpcv_gate',
    'brain_work:mine',
    'brain_work:promote',
    'brain_work:demote',
    'brain_work:regime_ledger',
    'brain_work:pattern_stats',
    'brain_work:breakout_outcomes',
    'brain_work:live_drift',
    'brain_work:execution_robustness'
)

function PsqlSelect($sql) {
    docker exec -e PGPASSWORD=chili $Postgres `
        psql -h localhost -U chili -d chili -t -A -F '|' -c $sql 2>&1
}

# ---------------------------------------------------------------- H1
Section 'H1 -- Is run_brain_work_dispatch_round running at all?'
Emit ''
Emit 'Brain-worker container state:'
$state = docker inspect $BrainWorker --format '{{.State.StartedAt}} status={{.State.Status}}'
Emit "  $state"

Emit ''
Emit 'Dispatcher log lines (since worker boot, both prefix variants):'
$dispLines = docker logs $BrainWorker 2>&1 |
    Select-String -Pattern 'brain_work_dispatch|brain_work:dispatch|work ledger dispatch round'
$nDisp = if ($null -eq $dispLines) { 0 } else { $dispLines.Count }
Emit "  total matching lines = $nDisp"
if ($nDisp -gt 0) {
    Emit '  --- first 3 ---'
    $dispLines | Select-Object -First 3 | ForEach-Object { Emit "    $_" }
    Emit '  --- last 3 ---'
    $dispLines | Select-Object -Last 3 | ForEach-Object { Emit "    $_" }
}

Emit ''
Emit 'Bootstrap call sites for run_brain_work_dispatch_round / run_brain_work_batch:'
$callSites = Select-String -Path 'C:\dev\chili-home-copilot\scripts\brain_worker.py' `
    -Pattern '_maybe_run_brain_work_batch|run_brain_work_batch\(' -SimpleMatch:$false
$callSites | ForEach-Object {
    Emit ("  {0}:{1}: {2}" -f $_.Filename, $_.LineNumber, $_.Line.Trim())
}

# ---------------------------------------------------------------- H2
Section 'H2 -- Logger filtered? Prefix mismatch?'
Emit ''
Emit 'Per-module LOG_PREFIX values (grepped):'
$prefixes = Select-String -Path 'C:\dev\chili-home-copilot\app\services\trading\brain_work\*.py' `
    -Pattern '^LOG_PREFIX\s*='
$prefixes += Select-String -Path 'C:\dev\chili-home-copilot\app\services\trading\brain_work\handlers\*.py' `
    -Pattern '^LOG_PREFIX\s*='
$prefixes | ForEach-Object {
    $rel = $_.Filename
    Emit ("  {0}: {1}" -f $rel, $_.Line.Trim())
}
Emit ''
Emit 'Observation: dispatcher LOG_PREFIX = "[brain_work_dispatch]" (UNDERSCORE)'
Emit 'while every handler uses "[brain_work:<name>]" (COLON).'
Emit 'Phase 0 audit grepped "brain_work:dispatch" -- the wrong separator.'
Emit ''
Emit 'Recount with underscore variant:'
$nUnderscore = (docker logs $BrainWorker 2>&1 | Select-String -Pattern 'brain_work_dispatch').Count
$nColon      = (docker logs $BrainWorker 2>&1 | Select-String -Pattern 'brain_work:dispatch').Count
Emit "  grep 'brain_work_dispatch' (underscore)  -> $nUnderscore"
Emit "  grep 'brain_work:dispatch' (colon, Ph0)  -> $nColon"

# ---------------------------------------------------------------- H3
Section 'H3 -- brain_work_ledger_enabled() False?'
Emit ''
$pyOut = docker exec $BrainWorker python -c @"
from app.db import SessionLocal
sess = SessionLocal()
try:
    from app.config import settings
    from app.services.trading.brain_work.ledger import brain_work_ledger_enabled
    print('brain_work_ledger_enabled_setting=' + str(getattr(settings, 'brain_work_ledger_enabled', None)))
    print('brain_work_ledger_enabled_call=' + str(brain_work_ledger_enabled()))
    print('brain_work_dispatch_batch_size=' + str(getattr(settings, 'brain_work_dispatch_batch_size', None)))
    print('brain_work_cpcv_gate_batch_size=' + str(getattr(settings, 'brain_work_cpcv_gate_batch_size', None)))
finally:
    try:
        sess.rollback()
    except Exception:
        pass
    sess.close()
"@ 2>&1
$pyOut | ForEach-Object { Emit "  $_" }

# ---------------------------------------------------------------- H4
Section 'H4 -- learning.py marking events done directly?'
Emit ''
Emit 'Searching app/services/trading/learning.py for BrainWorkEvent / brain_work_events refs:'
$lh = Select-String -Path 'C:\dev\chili-home-copilot\app\services\trading\learning.py' `
    -Pattern 'BrainWorkEvent|brain_work_events|status.*=.*[''"]done[''"]'
if ($null -eq $lh -or $lh.Count -eq 0) {
    Emit '  (no matches -- learning.py does not touch brain_work_events)'
} else {
    $lh | ForEach-Object { Emit ("  L{0}: {1}" -f $_.LineNumber, $_.Line.Trim()) }
}
Emit ''
Emit 'Repository-wide BrainWorkEvent / brain_work_events references (app/ only):'
$allRefs = Get-ChildItem -Path 'C:\dev\chili-home-copilot\app' -Recurse -Filter '*.py' |
    Select-String -Pattern 'brain_work_events|BrainWorkEvent'
$grouped = $allRefs | Group-Object -Property Filename | Sort-Object Name
$grouped | ForEach-Object {
    Emit ("  {0}: {1} hit(s)" -f $_.Name, $_.Count)
}

# ---------------------------------------------------------------- H5
Section 'H5 -- backtest_queue_worker emitting done directly?'
Emit ''
Emit 'Direct writes of brain_work_events.status outside ledger.py:'
$statusWrites = Get-ChildItem -Path 'C:\dev\chili-home-copilot\app' -Recurse -Filter '*.py' |
    Select-String -Pattern 'brain_work_events.*SET\s+status|UPDATE\s+brain_work_events|\.status\s*=\s*[''"]done[''"]' |
    Where-Object { $_.Filename -ne 'ledger.py' }
if ($null -eq $statusWrites -or $statusWrites.Count -eq 0) {
    Emit '  (none -- only ledger.py touches brain_work_events.status)'
} else {
    $statusWrites | ForEach-Object {
        Emit ("  {0}: L{1}: {2}" -f $_.Filename, $_.LineNumber, $_.Line.Trim())
    }
}
Emit ''
Emit 'enqueue_outcome_event INSERT writing status="done" directly (ledger.py):'
$insertSite = Select-String -Path 'C:\dev\chili-home-copilot\app\services\trading\brain_work\ledger.py' `
    -Pattern 'status=\"done\"|status=''done'''
$insertSite | ForEach-Object {
    Emit ("  ledger.py:L{0}: {1}" -f $_.LineNumber, $_.Line.Trim())
}
Emit ''
Emit 'Callers of enqueue_outcome_event / emit_backtest_completed_outcome:'
$callers = Get-ChildItem -Path 'C:\dev\chili-home-copilot\app' -Recurse -Filter '*.py' |
    Select-String -Pattern 'enqueue_outcome_event|emit_backtest_completed_outcome'
$callers | ForEach-Object {
    Emit ("  {0}:L{1}: {2}" -f $_.Filename, $_.LineNumber, $_.Line.Trim())
}

Emit ''
Emit 'DB evidence -- backtest_completed event_kind distribution (full history):'
$rows = PsqlSelect "SELECT event_type, event_kind, status, COUNT(*), MIN(created_at), MAX(created_at) FROM brain_work_events WHERE event_type='backtest_completed' GROUP BY event_type, event_kind, status ORDER BY event_kind, status;"
$rows | ForEach-Object { Emit "  $_" }

Emit ''
Emit 'DB evidence -- recent backtest_completed rows (last 24h, 5 most recent):'
$rows = PsqlSelect "SELECT id, event_type, event_kind, status, created_at, processed_at, lease_holder, attempts, payload->>'scan_pattern_id' as pid FROM brain_work_events WHERE event_type='backtest_completed' AND created_at > NOW() - INTERVAL '24 hours' ORDER BY created_at DESC LIMIT 5;"
$rows | ForEach-Object { Emit "  $_" }

# ---------------------------------------------------------------- H6
Section 'H6 -- Different handler consuming under different prefix?'
Emit ''
Emit 'Per-container handler log counts (since 24h):'
foreach ($c in $AllWorkers) {
    foreach ($p in $HandlerPrefixes) {
        $n = (docker logs --since 24h "chili-home-copilot-$c-1" 2>&1 | Select-String -Pattern $p).Count
        Emit ("  {0,-22} {1,-36} = {2}" -f $c, $p, $n)
    }
}
Emit ''
Emit 'Registered handler functions in dispatcher.py:'
$hwiring = Select-String -Path 'C:\dev\chili-home-copilot\app\services\trading\brain_work\dispatcher.py' `
    -Pattern 'elif event_type ==|from .handlers'
$hwiring | ForEach-Object {
    Emit ("  L{0}: {1}" -f $_.LineNumber, $_.Line.Trim())
}

# ---------------------------------------------------------------- per-type drain summary
Section 'Per-event-type drain summary (all event_types, 24h)'
$rows = PsqlSelect "SELECT event_type, event_kind, status, COUNT(*) FROM brain_work_events WHERE created_at > NOW() - INTERVAL '24 hours' GROUP BY event_type, event_kind, status ORDER BY event_type, event_kind, status;"
$rows | ForEach-Object { Emit "  $_" }

Section 'Per-event-type drain summary (all event_types, ALL TIME)'
$rows = PsqlSelect "SELECT event_type, event_kind, status, COUNT(*) FROM brain_work_events GROUP BY event_type, event_kind, status ORDER BY event_type, event_kind, status;"
$rows | ForEach-Object { Emit "  $_" }

# ---------------------------------------------------------------- claim_work_batch SQL
Section 'claim_work_batch SQL filter (proves work-only)'
Emit ''
$claim = Get-Content 'C:\dev\chili-home-copilot\app\services\trading\brain_work\ledger.py' |
    Select-String -Pattern 'event_kind\s*=\s*''work''' -SimpleMatch:$false
$claim | ForEach-Object { Emit "  ledger.py: $_" }

Section 'Verdict summary'
Emit ''
Emit 'See docs/AUDITS/2026-05-11_dispatcher_silence.md for full memo with H1-H6 verdicts.'
Emit ''
Emit "Audit run complete: $(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')"
