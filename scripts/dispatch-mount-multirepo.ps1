# Recreate scheduler-worker so the new multi-repo mounts (C:/dev:/host_dev
# and chili_dispatch_clones:/workspace_managed) take effect, then verify
# everything is in place.
#
# Output -> scripts/dispatch-mount-multirepo-output.txt
# Usage:  .\scripts\dispatch-mount-multirepo.ps1

$out = "scripts/dispatch-mount-multirepo-output.txt"
$start = Get-Date
"# dispatch-mount-multirepo $start" | Out-File $out -Encoding utf8

function Append { param([string]$Line) $Line | Add-Content $out }
function Section { param([string]$Title) Append ""; Append "===== $Title =====" }

Write-Host "[1/4] Recreating scheduler-worker (new volume mounts)..." -ForegroundColor Yellow
docker compose up -d --force-recreate scheduler-worker 2>&1 | Out-Null

Write-Host "[2/4] Waiting 25s for boot..." -ForegroundColor Yellow
Start-Sleep -Seconds 25

Write-Host "[3/4] Capturing diagnostic..." -ForegroundColor Yellow

Section "Container status"
docker compose ps scheduler-worker 2>&1 | Add-Content $out

Section "Mount /host_dev sanity (should show contents of C:\dev)"
docker compose exec -T scheduler-worker bash -c "ls /host_dev | head -25; echo total=`$(ls /host_dev | wc -l)" 2>&1 | Add-Content $out

Section "Mount /workspace_managed sanity (should exist, may be empty)"
docker compose exec -T scheduler-worker bash -c "test -d /workspace_managed && echo PRESENT && ls /workspace_managed | head -10 || echo MISSING" 2>&1 | Add-Content $out

Section "Mount /workspace sanity (chili-home-copilot itself)"
docker compose exec -T scheduler-worker bash -c "test -d /workspace/.git && echo 'PRESENT (.git)' || echo 'MISSING (.git)'" 2>&1 | Add-Content $out

Section "Resolver imports cleanly"
docker compose exec -T scheduler-worker python -c "
from app.services.code_brain import repo_resolver
print('parse_input:', callable(repo_resolver.parse_input))
print('resolve_or_register:', callable(repo_resolver.resolve_or_register))
# Quick parse smoke test
samples = [
    'C:\\\\dev\\\\example-project',
    'D:/code/foo',
    '/workspace',
    'https://github.com/MiacoRindolf/chili-home-copilot',
    'git@github.com:MiacoRindolf/chili-home-copilot.git',
    'MiacoRindolf/chili-home-copilot',
    'chili-home-copilot',
    '',
]
for s in samples:
    p = repo_resolver.parse_input(s)
    print(f'{s!r:60s} -> {p.kind.value}')
" 2>&1 | Add-Content $out

Section "Registered code_repos rows"
docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, name, host_path, container_path, path FROM code_repos ORDER BY id;" 2>&1 | Add-Content $out

Section "PAT presence (redacted)"
docker compose exec -T scheduler-worker bash -c "if [ -z `"`$CHILI_DISPATCH_GITHUB_TOKEN`" ]; then echo TOKEN=MISSING; else echo TOKEN=`${CHILI_DISPATCH_GITHUB_TOKEN:0:14}...; fi; echo PUSH_ENABLED=`$CHILI_DISPATCH_GIT_PUSH_ENABLED; echo REMOTE_USER=`$CHILI_DISPATCH_GIT_REMOTE_USER" 2>&1 | Add-Content $out

$elapsed = ((Get-Date) - $start).TotalSeconds
Append ""
Append "===== Done in $([Math]::Round($elapsed,1))s ====="

Write-Host "[4/4] Done -> $out" -ForegroundColor Green
Write-Host "Send 'ok' so Claude can read it."
