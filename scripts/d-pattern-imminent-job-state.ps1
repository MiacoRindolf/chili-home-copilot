$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-pattern-imminent-job-state-out.txt"
"# d-pattern-imminent-job-state $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"# scheduler-worker container status" | Add-Content $out
docker ps -a --filter "name=chili-home-copilot-scheduler-worker-1" --format "{{.Names}}: {{.Status}}" 2>&1 | Add-Content $out

"# scheduler-worker logs filtered for pattern_imminent_scanner" | Add-Content $out
docker logs --since 24h chili-home-copilot-scheduler-worker-1 2>&1 | Select-String -Pattern '(pattern_imminent|imminent_scanner|imminent_alert|run_pattern_imminent)' | Select-Object -First 40 | ForEach-Object { $_.Line } | Add-Content $out

"# scheduler-worker startup log -- which jobs got registered" | Add-Content $out
docker logs --since 24h chili-home-copilot-scheduler-worker-1 2>&1 | Select-String -Pattern '(Added job|trading_scheduler|scheduler started|CHILI_SCHEDULER_ROLE)' | Select-Object -First 30 | ForEach-Object { $_.Line } | Add-Content $out

"# scheduler-worker recent errors" | Add-Content $out
docker logs --since 24h chili-home-copilot-scheduler-worker-1 2>&1 | Select-String -Pattern '(ERROR|Traceback|Exception|FATAL|exception)' | Select-Object -First 20 | ForEach-Object { $_.Line } | Add-Content $out

"# scheduler-worker last 30 raw log lines" | Add-Content $out
docker logs --tail 30 chili-home-copilot-scheduler-worker-1 2>&1 | Add-Content $out

"# end" | Add-Content $out
