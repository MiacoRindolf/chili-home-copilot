$out = "scripts/dispatch-probe-bt-settings-output.txt"
"# bt settings $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "settings via brain-worker python" {
    docker compose exec -T brain-worker python -c "
from app.config import settings
print('brain_queue_batch_size=', settings.brain_queue_batch_size)
print('brain_backtest_parallel=', settings.brain_backtest_parallel)
print('brain_max_cpu_pct=', settings.brain_max_cpu_pct)
print('brain_queue_process_cap=', getattr(settings, 'brain_queue_process_cap', '<N/A>'))
print('brain_queue_backtest_executor=', getattr(settings, 'brain_queue_backtest_executor', '<N/A>'))
print('brain_queue_exploration_enabled=', getattr(settings, 'brain_queue_exploration_enabled', '<N/A>'))
print('brain_queue_exploration_max=', getattr(settings, 'brain_queue_exploration_max', '<N/A>'))
" 2>&1
}

S "container CPU limits" {
    docker stats --no-stream chili-home-copilot-brain-worker-1 chili-home-copilot-chili-1 chili-home-copilot-scheduler-worker-1 chili-home-copilot-autotrader-worker-1
}

S "brain-worker recent learning_cycle log lines" {
    docker compose logs brain-worker --tail 200 2>&1 | Select-String -Pattern "Queue backtest|backtest_queue|Patterns processed|max_workers" | Select-Object -Last 12
}

Write-Host "done"
