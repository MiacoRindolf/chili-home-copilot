# F-leak-1.5: Postgres integrity probe.
#
# Read-only diagnostic. Runs a series of consistency checks against
# tables the chili container writes, given chili has restarted 7x in
# 16h with status 'unhealthy' (potential mid-write interruption).
#
# Output: scripts/dispatch-postgres-integrity-output.txt
#
# Checks:
#   1. Orphaned rows: fast_executions / fast_exits without matching
#      lineage rows.
#   2. Long-held locks (pg_locks, granted=true, age > 5 min).
#   3. "Idle in transaction" connections (pg_stat_activity).
#   4. Row counts vs known-good snapshots (from f8a-evaluation reports).
#
# All read-only. Repair (if needed) is its own commit.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "scripts\dispatch-postgres-integrity-output.txt"
"# postgres integrity probe $(Get-Date -Format o)" | Out-File $out -Encoding utf8

function Probe([string]$label, [string]$sql) {
  "" | Add-Content $out
  "---$label---" | Add-Content $out
  docker compose exec -T postgres psql -U chili -d chili -c $sql 2>&1 | Add-Content $out
}

# 1. fast_executions referencing fast_alerts: rows where the join
# misses (i.e., entry was placed but no alert row exists with the
# matching denormalised triple). Inherited bootstrap entries genuinely
# have no alert -- known-OK cohort. We separate them out.
Probe "1a. fast_executions with NO matching fast_alerts (since F8a-fix, id > 2300)" @"
SELECT COUNT(*) AS orphan_executions
FROM fast_executions e
LEFT JOIN fast_alerts a
  ON a.ticker = e.ticker
 AND a.alert_type = e.alert_type
 AND a.fired_at = e.alert_fired_at
WHERE a.id IS NULL
  AND e.id > 2300;
"@

Probe "1b. fast_executions with NO matching fast_alerts (full history; expect ~11 from inherited bootstrap)" @"
SELECT COUNT(*) AS orphan_executions_total
FROM fast_executions e
LEFT JOIN fast_alerts a
  ON a.ticker = e.ticker
 AND a.alert_type = e.alert_type
 AND a.fired_at = e.alert_fired_at
WHERE a.id IS NULL;
"@

Probe "1c. fast_exits with NO matching fast_executions (broken lineage)" @"
SELECT COUNT(*) AS orphan_exits
FROM fast_exits x
LEFT JOIN fast_executions e ON e.id = x.entry_execution_id
WHERE e.id IS NULL;
"@

Probe "1d. fast_executions paper_fill with NO matching fast_exits AND > 4h old (zombie open positions)" @"
SELECT COUNT(*) AS zombie_open_positions
FROM fast_executions e
LEFT JOIN fast_exits x ON x.entry_execution_id = e.id
WHERE e.decision = 'paper_fill'
  AND e.mode = 'paper'
  AND x.id IS NULL
  AND e.decided_at < NOW() - INTERVAL '4 hours';
"@

# 2. Long-held locks
Probe "2a. pg_locks granted=true held > 5 min on critical tables" @"
SELECT l.relation::regclass AS table_, l.mode, l.granted,
       a.pid, a.state, NOW() - a.xact_start AS xact_age,
       a.query
FROM pg_locks l
JOIN pg_stat_activity a ON a.pid = l.pid
WHERE l.granted = true
  AND a.state IS NOT NULL
  AND (NOW() - COALESCE(a.xact_start, a.query_start)) > INTERVAL '5 minutes'
  AND l.relation::regclass::text LIKE 'fast_%'
ORDER BY xact_age DESC NULLS LAST
LIMIT 20;
"@

Probe "2b. pg_locks granted=false (waiting locks)" @"
SELECT relation::regclass AS table_, mode, granted, pid
FROM pg_locks
WHERE granted = false
LIMIT 20;
"@

# 3. Idle-in-transaction
Probe "3. pg_stat_activity 'idle in transaction' > 1 min (potential leak from chili restart)" @"
SELECT pid, state, NOW() - state_change AS idle_age,
       application_name, client_addr, query
FROM pg_stat_activity
WHERE state = 'idle in transaction'
  AND (NOW() - state_change) > INTERVAL '1 minute'
ORDER BY idle_age DESC
LIMIT 20;
"@

# 4. Row counts vs known-good
Probe "4a. fast_alerts rowcount + max id (known-good 2026-05-03 04:35: 191 post-fix pullback alerts)" @"
SELECT
  COUNT(*) AS total_alerts,
  MAX(id) AS max_id,
  COUNT(*) FILTER (WHERE alert_type = 'volume_breakout_pullback_long' AND id > 2300) AS post_fix_pullback,
  COUNT(*) FILTER (WHERE alert_type = 'volume_breakout_pullback_long') AS all_pullback
FROM fast_alerts;
"@

Probe "4b. fast_executions rowcount (known-good: 142 closed pullback round trips)" @"
SELECT
  COUNT(*) AS total_executions,
  COUNT(*) FILTER (WHERE decision = 'paper_fill') AS paper_fills,
  COUNT(*) FILTER (WHERE decision = 'rejected') AS rejected,
  COUNT(*) FILTER (WHERE decision = 'live_placed') AS live_placed
FROM fast_executions;
"@

Probe "4c. fast_exits rowcount + matching pullback exits" @"
SELECT
  COUNT(*) AS total_exits,
  COUNT(*) FILTER (WHERE entry_execution_id IN (
    SELECT e.id FROM fast_executions e
    JOIN fast_alerts a ON a.ticker=e.ticker
                      AND a.alert_type=e.alert_type
                      AND a.fired_at=e.alert_fired_at
    WHERE a.alert_type = 'volume_breakout_pullback_long'
  )) AS pullback_exits
FROM fast_exits;
"@

Probe "4d. fast_signal_decay rowcount (known-good 2026-05-03 04:35: 101 pullback cells)" @"
SELECT
  COUNT(*) AS total_cells,
  COUNT(*) FILTER (WHERE alert_type = 'volume_breakout_pullback_long') AS pullback_cells,
  SUM(sample_count) AS total_obs
FROM fast_signal_decay;
"@

# 5. Pair-status snapshot (sanity vs F-hygiene-1)
Probe "5. fast_path_status (should be all streaming, last_error NULL post F-hygiene-1)" @"
SELECT ticker, state, last_error, updated_at
FROM fast_path_status ORDER BY ticker;
"@

Write-Output "done"
