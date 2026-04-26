# Quick read-only snapshot of the reactive Code Brain. No restart, no LLM
# calls, no DB writes. Run anytime to see what's happening.
#
# Usage: .\scripts\brain-status.ps1

Write-Host "=== Code Brain Status ===" -ForegroundColor Cyan
Write-Host ""

Write-Host "[Container]" -ForegroundColor Yellow
docker compose ps scheduler-worker
Write-Host ""

Write-Host "[Runtime state]" -ForegroundColor Yellow
docker compose exec -T postgres psql -U chili -d chili -c @"
SELECT mode,
       daily_premium_usd_cap AS cap,
       spent_today_usd       AS spent,
       (daily_premium_usd_cap - spent_today_usd) AS remaining,
       template_min_confidence AS tmpl_min,
       novelty_premium_threshold AS novelty_th,
       local_model_promoted    AS local_promoted,
       last_pattern_mining_at  AS last_mine
FROM code_brain_runtime_state;
"@
Write-Host ""

Write-Host "[Event queue]" -ForegroundColor Yellow
docker compose exec -T postgres psql -U chili -d chili -c @"
SELECT
  COUNT(*) FILTER (WHERE claimed_at IS NULL)                  AS unclaimed,
  COUNT(*) FILTER (WHERE claimed_at IS NOT NULL AND processed_at IS NULL) AS in_flight,
  COUNT(*) FILTER (WHERE processed_at IS NOT NULL AND outcome = 'success')   AS processed_ok,
  COUNT(*) FILTER (WHERE processed_at IS NOT NULL AND outcome = 'escalated') AS processed_escalated,
  COUNT(*) FILTER (WHERE processed_at IS NOT NULL AND outcome = 'failure')   AS processed_failed,
  COUNT(*) FILTER (WHERE processed_at IS NOT NULL AND outcome = 'skipped')   AS processed_skipped
FROM code_brain_events;
"@
Write-Host ""

Write-Host "[Decisions in last 24h, by type]" -ForegroundColor Yellow
docker compose exec -T postgres psql -U chili -d chili -c @"
SELECT decision,
       COUNT(*)              AS n,
       SUM(cost_usd)         AS total_cost_usd,
       SUM(llm_tokens_used)  AS total_tokens
FROM code_decision_router_log
WHERE decided_at > NOW() - INTERVAL '24 hours'
GROUP BY decision
ORDER BY n DESC;
"@
Write-Host ""

Write-Host "[Last 10 routing decisions]" -ForegroundColor Yellow
docker compose exec -T postgres psql -U chili -d chili -c @"
SELECT id,
       decided_at,
       task_id,
       decision,
       outcome,
       cost_usd,
       LEFT(rule_snapshot::text, 80) AS rule_snippet
FROM code_decision_router_log
ORDER BY id DESC LIMIT 10;
"@
Write-Host ""

Write-Host "[Patterns mined]" -ForegroundColor Yellow
docker compose exec -T postgres psql -U chili -d chili -c @"
SELECT id, name, confidence, success_count, failure_count, last_used_at
FROM code_patterns
ORDER BY confidence DESC, success_count DESC LIMIT 10;
"@
Write-Host ""

Write-Host "[Plan tasks ready for dispatch]" -ForegroundColor Yellow
docker compose exec -T postgres psql -U chili -d chili -c @"
SELECT id, title, coding_readiness_state, sort_order
FROM plan_tasks
WHERE coding_readiness_state = 'ready_for_dispatch'
ORDER BY id DESC LIMIT 10;
"@
Write-Host ""

Write-Host "Done." -ForegroundColor Green
