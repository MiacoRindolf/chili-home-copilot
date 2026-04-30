# Dump chili startup logs to find why migrations 207/208 didn't run.
$out = "scripts/dispatch-chili-mig-error-output.txt"
"# Chili migration error trace $(Get-Date)" | Out-File $out -Encoding utf8

function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "chili health" {
    docker inspect chili-home-copilot-chili-1 --format '{{.State.Health.Status}}: {{json .State.Health.Log}}'
}

S "chili last 200 log lines" {
    docker compose logs chili --tail 200 2>&1
}

S "chili startup migration lines" {
    docker compose logs chili 2>&1 | Select-String -Pattern "Migration|migration_2|schema_version|Failed|Exception|Traceback" | Select-Object -Last 80
}

Write-Host "done"
