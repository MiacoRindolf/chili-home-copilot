$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectRoot

$Cert = "localhost+2.pem"
$Key = "localhost+2-key.pem"

if (-not (Test-Path $Cert) -or -not (Test-Path $Key)) {
    Write-Host "Certificates not found. Generating..." -ForegroundColor Yellow

    if (-not (Test-Path "mkcert.exe")) {
        Write-Host "Downloading mkcert..." -ForegroundColor Yellow
        $url = "https://github.com/FiloSottile/mkcert/releases/download/v1.4.4/mkcert-v1.4.4-windows-amd64.exe"
        Invoke-WebRequest -Uri $url -OutFile "mkcert.exe"
    }

    .\mkcert.exe -install

    $LanIP = (Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.*' } |
        Select-Object -First 1).IPAddress

    Write-Host "LAN IP: $LanIP" -ForegroundColor Cyan
    .\mkcert.exe localhost 127.0.0.1 $LanIP
}

$LanIP = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.*' } |
    Select-Object -First 1).IPAddress

Write-Host ""
Write-Host "=== CHILI Home Copilot (HTTPS) ===" -ForegroundColor Green
Write-Host "  Local:  https://localhost:8000/chat" -ForegroundColor Cyan
Write-Host "  LAN:    https://${LanIP}:8000/chat" -ForegroundColor Cyan
Write-Host ""

conda run -n chili-env uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 --ssl-certfile $Cert --ssl-keyfile $Key
