# What is actually listening on host port 8000 — plain HTTP or HTTPS?
# Run from repo root: .\scripts\diagnose-docker-8000.ps1
$ErrorActionPreference = "Continue"
Write-Host ""
Write-Host "=== CHILI port 8000 (host) ===" -ForegroundColor Cyan

Write-Host "`n[1] Docker: chili container" -ForegroundColor Yellow
try {
    docker compose ps chili 2>&1
} catch {
    Write-Host "docker compose failed: $_" -ForegroundColor Red
}

Write-Host "`n[2] Last entrypoint / uvicorn lines (TLS hint)" -ForegroundColor Yellow
try {
    docker compose logs chili --tail 25 2>&1
} catch {
    Write-Host "docker logs failed: $_" -ForegroundColor Red
}

Write-Host "`n[3] Probe HTTP vs HTTPS (curl.exe)" -ForegroundColor Yellow
$curl = "$env:SystemRoot\System32\curl.exe"
if (-not (Test-Path $curl)) {
    Write-Host "curl.exe not found; install or use WSL." -ForegroundColor Red
    exit 1
}

Write-Host "  HTTP  GET http://127.0.0.1:8000/health ..."
try {
    & $curl -sS -m 5 -o NUL -w "HTTP status: %{http_code}`n" "http://127.0.0.1:8000/health" 2>&1
} catch {
    Write-Host "  HTTP probe failed: $_" -ForegroundColor DarkGray
}

Write-Host "  HTTPS GET https://127.0.0.1:8000/health (-k) ..."
try {
    & $curl -k -sS -m 5 -o NUL -w "HTTP status: %{http_code}`n" "https://127.0.0.1:8000/health" 2>&1
} catch {
    Write-Host "  HTTPS probe failed: $_" -ForegroundColor DarkGray
}

Write-Host "`n[4] How to read this" -ForegroundColor Yellow
Write-Host "  - If ONLY http:// returns 200: server is PLAIN HTTP -> open http://localhost:8000/brain (not https://)." -ForegroundColor Gray
Write-Host "  - If ONLY https:// returns 200: server is HTTPS -> open https://127.0.0.1:8000/brain and accept the cert." -ForegroundColor Gray
Write-Host "  - If both fail: nothing is listening on 8000 or firewall; check docker compose ps." -ForegroundColor Gray
Write-Host ""
