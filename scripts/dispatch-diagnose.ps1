# Dispatch diagnostic snapshot — captures container env, recent dispatch state,
# and relevant log slices to scripts/dispatch-diagnose-output.txt.
#
# Usage: .\scripts\dispatch-diagnose.ps1
# Then send "ok" to Claude — Claude reads scripts/dispatch-diagnose-output.txt directly.

$out = "scripts/dispatch-diagnose-output.txt"
$start = Get-Date
"# Dispatch diagnostic snapshot $start" | Out-File $out -Encoding utf8

function Write-Section {
    param([string]$Title, [scriptblock]$Body)
    "" | Add-Content $out
    "===== $Title =====" | Add-Content $out
    try {
        $result = & $Body 2>&1
        if ($result) { $result | Out-String | Add-Content $out }
    } catch {
        "ERROR running section: $_" | Add-Content $out
    }
}

Write-Section "Containers" {
    docker ps --format "table {{.Names}}`t{{.Status}}`t{{.Ports}}"
}

Write-Section "scheduler-worker /workspace mount sanity" {
    "--- /workspace listing ---"
    docker compose exec -T scheduler-worker ls -la /workspace 2>&1 | Select-Object -First 8
    ""
    "--- /workspace/.git presence ---"
    docker compose exec -T scheduler-worker bash -c "test -d /workspace/.git && echo 'PRESENT' || echo 'MISSING'" 2>&1
    ""
    "--- git -C /workspace status ---"
    docker compose exec -T scheduler-worker bash -c "cd /workspace && git status --short 2>&1 | head -5; echo 'exit='$?" 2>&1
    ""
    "--- git -C /workspace branch verify main ---"
    docker compose exec -T scheduler-worker bash -c "cd /workspace && git rev-parse --verify main 2>&1; echo 'exit='$?" 2>&1
    ""
    "--- existing worktrees ---"
    docker compose exec -T scheduler-worker bash -c "cd /workspace && git worktree list 2>&1" 2>&1
    ""
    "--- /workspace/.git/worktrees subdir listing ---"
    docker compose exec -T scheduler-worker bash -c "ls -la /workspace/.git/worktrees/ 2>&1 | head -20" 2>&1
    ""
    "--- direct dispatch worktree add reproduction (verbose) ---"
    docker compose exec -T scheduler-worker bash -c "rm -rf /tmp/chili-dispatch/task-test && cd /workspace && git worktree prune -v 2>&1; git worktree add -B dispatch/test /tmp/chili-dispatch/task-test main 2>&1; echo 'add_exit='$?; git worktree remove --force /tmp/chili-dispatch/task-test 2>&1; echo 'cleanup_exit='$?" 2>&1
}

Write-Section "code_repos.container_path for chili-home-copilot" {
    docker compose exec -T postgres psql -U chili -d chili -c `
      "SELECT id, name, host_path, container_path, path FROM code_repos WHERE name='chili-home-copilot';"
}

Write-Section "Scheduler-worker env (dispatch-relevant)" {
    # Dump full env, filter PowerShell-side. Avoids bash -c parens parsing pain.
    $raw = docker compose exec -T scheduler-worker printenv 2>&1
    $patterns = @(
        '^CHILI_DISPATCH_',
        '^CHILI_SCHEDULER_ROLE',
        '^PREMIUM_',
        '^LLM_API_KEY',
        '^LLM_MODEL',
        '^LLM_BASE_URL',
        '^OPENAI_API_KEY',
        '^PAID_OPENAI_'
    )
    $rx = [string]::Join('|', $patterns)
    $raw -split "`r?`n" | Where-Object { $_ -match $rx } | ForEach-Object {
        # Redact secrets: only show first 8 chars of any *_API_KEY value.
        if ($_ -match '^([A-Z0-9_]+API_KEY)=(.{0,8}).*') {
            "$($Matches[1])=$($Matches[2])... [redacted]"
        } else {
            $_
        }
    } | Sort-Object
}

Write-Section "Last 50 dispatch-relevant log lines" {
    docker compose logs scheduler-worker --tail 1000 2>&1 |
        Select-String -Pattern "code_dispatch|code-agent|gemini|llm_reply|model_not_found|RateLimit|insufficient_quota|auth_failed|Traceback|scheduler_worker.*Started" |
        Select-Object -Last 50
}

Write-Section "Full tracebacks + 20 lines of context after each" {
    # Capture raw lines so we can include traceback bodies (file paths, error messages)
    # that the filtered section above truncates.
    $lines = @(docker compose logs scheduler-worker --tail 2000 2>&1 | ForEach-Object { $_.ToString() })
    $tbIndices = @()
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "Traceback \(most recent call last\)") { $tbIndices += $i }
    }
    if ($tbIndices.Count -eq 0) {
        "(no tracebacks found in last 2000 log lines)"
    } else {
        foreach ($idx in $tbIndices) {
            $end = [Math]::Min($idx + 20, $lines.Count - 1)
            "----- Traceback at log line $idx -----"
            for ($j = $idx; $j -le $end; $j++) { $lines[$j] }
            ""
        }
    }
}

Write-Section "Last 30 ERROR/WARNING/Exception lines" {
    docker compose logs scheduler-worker --tail 2000 2>&1 |
        Select-String -Pattern "\[ERROR\]|\[WARNING\]|\[CRITICAL\]|Exception|Error:" |
        Select-Object -Last 30
}

Write-Section "code_agent_runs (last 10 minutes)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, started_at, finished_at, cycle_step, decision, task_id, LEFT(escalation_reason, 200) AS escalation FROM code_agent_runs WHERE started_at > NOW() - INTERVAL '10 minutes' ORDER BY id DESC LIMIT 15;"
}

Write-Section "coding_agent_suggestion (last 5)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, task_id, model, LEFT(response_text, 300) AS response, LEFT(diffs_json, 300) AS diffs FROM coding_agent_suggestion ORDER BY id DESC LIMIT 5;"
}

Write-Section "llm_call_log (last 10 minutes)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, created_at, provider, model, success, weak_response, failure_kind, latency_ms, tokens_in, tokens_out, LEFT(completion, 200) AS preview FROM llm_call_log WHERE created_at > NOW() - INTERVAL '10 minutes' ORDER BY id DESC LIMIT 10;"
}

Write-Section "plan_tasks (active queue)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, title, coding_readiness_state, sort_order FROM plan_tasks WHERE coding_readiness_state = 'ready_for_dispatch' ORDER BY sort_order ASC, id DESC LIMIT 10;"
}

Write-Section "code_kill_switch_state" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT * FROM code_kill_switch_state;"
}

Write-Section "trading_risk_state (latest)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT regime, breaker_tripped, snapshot_date FROM trading_risk_state ORDER BY snapshot_date DESC LIMIT 1;"
}

$elapsed = ((Get-Date) - $start).TotalSeconds
"" | Add-Content $out
"===== Done in $([Math]::Round($elapsed,1))s =====" | Add-Content $out

Write-Host "Diagnostic written to $out ($([Math]::Round($elapsed,1))s)"
