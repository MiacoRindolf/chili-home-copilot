$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectRoot

# Default 8000. Port 5000 was only used by the removed start-https-5000.ps1 - treat as stale env.
$ListenPort = 8000
if ($env:CHILI_PORT -match '^\d+$') {
    $ListenPort = [int]$env:CHILI_PORT
}
if ($ListenPort -eq 5000) {
    Write-Host "Ignoring CHILI_PORT=5000 (project uses 8000). For a different port use e.g. CHILI_PORT=8010." -ForegroundColor Yellow
    $ListenPort = 8000
    Remove-Item Env:CHILI_PORT -ErrorAction SilentlyContinue
} elseif ($env:CHILI_PORT -match '^\d+$') {
    Write-Host "Using CHILI_PORT=$ListenPort (override). Default is 8000 - run: Remove-Item Env:CHILI_PORT" -ForegroundColor DarkGray
}

# Free port (self-elevates only if needed). Excluded Hyper-V ranges exit 1 - set CHILI_PORT=8010 or use start-dev.ps1
# Run twice with delay so slow-to-exit listeners are cleared (avoids 10048 after "Port is free").
& "$PSScriptRoot\free-port.ps1" -Port $ListenPort
if ($LASTEXITCODE -ne 0) {
    Write-Host "free-port.ps1 failed (exit $LASTEXITCODE). Port $ListenPort not usable." -ForegroundColor Red
    Write-Host "Try: `$env:CHILI_PORT='8010'; .\scripts\start-https.ps1" -ForegroundColor Yellow
    Write-Host "Or HTTP: .\scripts\start-dev.ps1" -ForegroundColor Yellow
    Write-Host "Run: .\scripts\diagnose-port-8000.ps1 -Port $ListenPort" -ForegroundColor Yellow
    exit 1
}
Start-Sleep -Seconds 2
& "$PSScriptRoot\free-port.ps1" -Port $ListenPort
if ($LASTEXITCODE -ne 0) {
    Write-Host "free-port.ps1 failed on second pass (exit $LASTEXITCODE). Port $ListenPort not usable." -ForegroundColor Red
    Write-Host "Run: .\scripts\diagnose-port-8000.ps1 -Port $ListenPort" -ForegroundColor Yellow
    exit 1
}

# Prefer stable paths under certs/ (matches docs). mkcert writes here with -cert-file/-key-file
# so uvicorn always uses the files we just generated (older script left $Cert pointing at
# localhost+2.pem while mkcert created differently named files).
$Cert = "certs/localhost.pem"
$Key = "certs/localhost.key"
$LegacyCert = "localhost+2.pem"
$LegacyKey = "localhost+2-key.pem"

if (-not ((Test-Path $Cert) -and (Test-Path $Key))) {
    if ((Test-Path $LegacyCert) -and (Test-Path $LegacyKey)) {
        $Cert = $LegacyCert
        $Key = $LegacyKey
        Write-Host "Using legacy cert files: $Cert (consider migrating to certs/localhost.pem)" -ForegroundColor DarkGray
    }
}

if (-not ((Test-Path $Cert) -and (Test-Path $Key))) {
    Write-Host "Certificates not found. Generating trusted dev certs (mkcert) -> certs\ ..." -ForegroundColor Yellow
    New-Item -ItemType Directory -Force -Path "certs" | Out-Null

    $mkcert = Join-Path $PSScriptRoot "tools\mkcert.exe"
    if (-not (Test-Path $mkcert)) {
        New-Item -ItemType Directory -Force -Path (Split-Path $mkcert) | Out-Null
        Write-Host "Downloading mkcert to scripts\tools\ ..." -ForegroundColor Yellow
        $url = "https://github.com/FiloSottile/mkcert/releases/download/v1.4.4/mkcert-v1.4.4-windows-amd64.exe"
        Invoke-WebRequest -Uri $url -OutFile $mkcert
    }

    Write-Host "Installing local CA (Administrator approval may be required once)..." -ForegroundColor Yellow
    & $mkcert -install
    if ($LASTEXITCODE -ne 0) {
        Write-Host "mkcert -install failed. Open PowerShell as Administrator and run: `"$mkcert`" -install" -ForegroundColor Red
        exit 1
    }

    $LanIP = (Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.*' } |
        Select-Object -First 1).IPAddress

    Write-Host "LAN IP (optional SAN): $LanIP" -ForegroundColor Cyan
    $Cert = "certs/localhost.pem"
    $Key = "certs/localhost.key"
    if ($LanIP) {
        & $mkcert -key-file $Key -cert-file $Cert localhost 127.0.0.1 ::1 $LanIP
    } else {
        & $mkcert -key-file $Key -cert-file $Cert localhost 127.0.0.1 ::1
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "mkcert failed to write $Cert" -ForegroundColor Red
        exit 1
    }
}

$LanIP = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.*' } |
    Select-Object -First 1).IPAddress

Write-Host ""
Write-Host "=== CHILI Home Copilot (HTTPS) ===" -ForegroundColor Green
Write-Host "  Local:  https://localhost:${ListenPort}/chat" -ForegroundColor Cyan
Write-Host "  LAN:    https://${LanIP}:${ListenPort}/chat" -ForegroundColor Cyan
Write-Host ""

conda run -n chili-env uvicorn app.main:app --reload --host 0.0.0.0 --port $ListenPort --ssl-certfile $Cert --ssl-keyfile $Key
