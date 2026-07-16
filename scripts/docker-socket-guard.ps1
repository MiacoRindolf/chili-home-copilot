# Docker socket-leak guard (2026-07-10, ang -$23k GMM incident root cause).
# com.docker.backend leaks TCP sockets (~360/h dokumentado); pagkaubos ng ephemeral
# ports (Event 4231) ang worker ay BUHAY pero hindi na maka-abot sa broker — hindi
# makapag-exit ng posisyon. Ang guard na ito (naka-schedule kada 30 min):
#   * binibilang ang TCP connections; kapag lagpas sa threshold AT flat ang account,
#     gumagawa ng GRACEFUL `docker desktop restart` (nire-reset ang leak);
#   * kapag lagpas ang threshold pero MAY posisyon, nag-lo-log lang ng ALERT (huwag
#     i-restart ang stack habang may hawak) — ang broker-side dead-man stop ang sahod.
# Log: D:\CHILI-Docker\chili-data\socket-guard.log

$ErrorActionPreference = 'SilentlyContinue'
$log = 'D:\CHILI-Docker\chili-data\socket-guard.log'
$THRESHOLD = 10000   # ~60% ng default 16,384 dynamic ports; may headroom pa para sa restart
function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Out-File -FilePath $log -Append -Encoding utf8 }

$count = (Get-NetTCPConnection -ErrorAction SilentlyContinue | Measure-Object).Count
if ($count -lt $THRESHOLD) { exit 0 }   # healthy — tahimik na exit, walang log spam

Log "TCP connections=$count lagpas sa threshold=$THRESHOLD"

# May hawak bang posisyon? (Alpaca paper API; keys mula sa deploy .env)
$envFile = 'D:\dev\chili-home-copilot\.env'
$paperMatch = Select-String -Path $envFile -Pattern '^CHILI_ALPACA_PAPER=(.+)$' | Select-Object -First 1
$paperRaw = if ($paperMatch) { $paperMatch.Matches[0].Groups[1].Value.Trim().Trim('"').Trim("'").ToLowerInvariant() } else { '' }
if ($paperRaw -notin @('1', 'true', 'yes', 'on')) {
    # Never query even the hard-coded paper endpoint when process posture is live
    # or ambiguous. A legacy live holding cannot be disproved by paper-flat truth,
    # so restarting Docker here would be unsafe.
    Log "Alpaca paper posture not explicitly certified -- no broker call; treating as NOT flat"
    exit 0
}
$key = (Select-String -Path $envFile -Pattern '^CHILI_ALPACA_API_KEY=(.+)$').Matches[0].Groups[1].Value.Trim()
$sec = (Select-String -Path $envFile -Pattern '^CHILI_ALPACA_API_SECRET=(.+)$').Matches[0].Groups[1].Value.Trim()
$flat = $true
try {
    $resp = Invoke-RestMethod -Uri 'https://paper-api.alpaca.markets/v2/positions' -Headers @{ 'APCA-API-KEY-ID' = $key; 'APCA-API-SECRET-KEY' = $sec } -TimeoutSec 20
    if ($resp -and @($resp).Count -gt 0) { $flat = $false }
} catch {
    Log "positions check failed ($_) -- treating as NOT flat (conservative)"
    $flat = $false
}

if (-not $flat) {
    Log "ALERT: socket pressure habang MAY posisyon -- hindi magre-restart; umaasa sa dead-man stop"
    exit 0
}

Log "flat + socket pressure -> graceful docker desktop restart"
& 'C:\Program Files\Docker\Docker\resources\bin\docker.exe' desktop restart *> $null
Start-Sleep -Seconds 60
# pagkarestart ng daemon, siguruhing buo ulit ang stack
& powershell -NoProfile -ExecutionPolicy Bypass -File 'D:\dev\chili-home-copilot\scripts\start-chili-stack.ps1'
Log "restart + stack recovery done"
