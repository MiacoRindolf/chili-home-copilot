# CHILI stack self-recovery (2026-07-10, post socket-exhaustion incident).
# Boots the WHOLE trading stack unattended: Docker Desktop -> daemon -> postgres FIRST
# -> momentum-exec-worker (its orphan reconciler is the position-safety net) -> the rest.
# Registered at logon via CHILI-Stack-Recovery (run-hidden.vbs, same pattern as the
# IQFeed bridge tasks). Idempotent: safe to run any time; running pieces are left alone.
# Log: D:\CHILI-Docker\chili-data\stack-recovery.log

$ErrorActionPreference = 'SilentlyContinue'
$log = 'D:\CHILI-Docker\chili-data\stack-recovery.log'
function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Out-File -FilePath $log -Append -Encoding utf8 }

Log "=== stack recovery pass start ==="

# 1) Docker Desktop (the daemon does NOT auto-start on this box)
if (-not (Get-Process 'com.docker.backend' -ErrorAction SilentlyContinue)) {
    Log "docker backend not running -> launching Docker Desktop"
    Start-Process -FilePath 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
}

# 2) wait for the daemon (up to 5 min)
$deadline = (Get-Date).AddMinutes(5)
while ((Get-Date) -lt $deadline) {
    docker version --format '{{.Server.Version}}' *> $null
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 10
}
docker version --format '{{.Server.Version}}' *> $null
if ($LASTEXITCODE -ne 0) { Log "FATAL: daemon never came up"; exit 1 }
Log "daemon up"

# 3) postgres FIRST (everything depends on it)
docker start chili-home-copilot-postgres-1 *> $null
$deadline = (Get-Date).AddMinutes(3)
while ((Get-Date) -lt $deadline) {
    docker exec chili-home-copilot-postgres-1 pg_isready -U chili *> $null
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 5
}
Log "postgres ready"

# 4) the momentum exec worker (compose-canonical; carries the orphan reconciler)
Set-Location 'D:\dev\chili-home-copilot'
docker compose --profile live-momentum up -d --no-deps momentum-exec-worker *> $null
Log "momentum-exec-worker up"

# 5) the rest of the stack (order-insensitive)
docker start chili-clean-recovery-web chili-clean-recovery-scheduler chili-clean-recovery-brain chili-cloudflare-origin-bridge chili-home-copilot-ollama-1 *> $null
Log "supporting containers started"

# 6) verify the deploy landmine: EXACTLY ONE DATABASE_URL in the worker
$n = docker exec chili-clean-recovery-momentum-exec sh -c 'env | grep -c "^DATABASE_URL="'
Log "worker DATABASE_URL count=$n (dapat 1)"

Log "=== stack recovery pass done ==="
