# IQConnect watchdog (2026-08-23, binago 18:5x).
#
# ANG PROBLEMA: nagsa-shutdown ang IQConnect pagkatapos mawala ang HULING client
# ("LAST CLIENT DISCONNECTED. SHUTTING DOWN IN N SEC"). Kapag sabay na
# nadiskonekta ang dalawang bridge kahit saglit -- reconnect, restart, blip --
# nawawala ito, at WALANG nagre-restart: ang mga launcher ay tumatakbo lang sa
# logon at 03:55. Ang natitira ay paikot-ikot sa WinError 10061 = TAHIMIK na
# data blackout. Nangyari nang 4x noong gabi ng 2026-08-23.
#
# !! ANG UNANG LUNAS AY NASA REGISTRY, HINDI DITO:
#   HKCU:\Software\DTN\IQFeed\Startup\ShutdownDelayLastClient  (default 5 seg)
# Itinaas ito sa 300 seg noong 2026-08-23, kaya hindi na namamatay ang IQConnect
# sa normal na reconnect blip. Ang watchdog na ito ay PANGALAWANG linya lang.
#
# DALAWANG TAMANG BAGAY NA KAILANGAN NG IQCONNECT (natutunan nang mahirap):
#   1) CREDENTIALS sa registry -- kung wala, "Waiting for user or server list"
#      at nakatigil ito sa GUI login dialog HABAMBUHAY. WALANG saysay ang
#      pag-restart; kailangan ng TAO. Kaya may guard ang script na ito.
#   2) PRODUCT ID sa command line -- kung wala, umaabot ito sa "0 products left
#      to authenticate. Ready" at TUMITIGIL doon: hindi kailanman kumokonekta sa
#      quote server, at ang bridges ay makakakita ng "S,SERVER DISCONNECTED"
#      magpakailanman habang mukhang malusog ang lahat.
#
# !! ITINAMA (2026-08-23): dati kong isinulat na ang -product IQFEED_CHARTS ang
# sanhi ng "selected-field acknowledgement mismatch". MALI IYON. Ang mismatch ay
# lumilitaw kapag WALANG upstream -- hindi maaaring kumpirmahin ng IQConnect ang
# field roster kung hindi ito konektado sa quote server. Ang malusog na session
# noong 09:38-16:14 (31,775 Connected) ay may 2 authenticated products.
#
# ANG GINAGAWA: kung may bridge PERO walang IQConnect, i-start ito nang MAY
# product. WALANG pinapatay kailanman -- additive lang.

$ErrorActionPreference = 'SilentlyContinue'
$logDir = 'D:\CHILI-Docker\chili-data\iqfeed_trades'
$wlog = Join-Path $logDir 'iqconnect-watchdog.log'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

$bridges = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like '*iqfeed_trade_bridge.py*' -or
                   $_.CommandLine -like '*iqfeed_depth_bridge.py*' })
$iq = Get-Process iqconnect -ErrorAction SilentlyContinue

if ($bridges.Count -eq 0) { exit 0 }   # walang bridge -- wala tayong dapat gawin
if ($iq) { exit 0 }                    # maayos ang lahat

# GUARD: kung walang naka-save na credentials, ang pag-start ay walang kuwenta --
# titigil lang ito sa login dialog. Mag-log NANG ISANG BESES kada 30 min para
# hindi mapuno ang log, at huwag mag-thrash.
# Tingnan ang LAHAT NG TATLO. Ang Login lang ay FAIL-OPEN: noong 2026-08-23
# ang Login ay nanatiling nakatakda habang ang Password ay nabura at ang
# AutoConnect ay naging false -- lumusot sana ang guard at magre-restart
# nang walang saysay habang nakatigil ang IQConnect sa login dialog.
$st = Get-ItemProperty 'HKCU:\Software\DTN\IQFeed\Startup' -ErrorAction SilentlyContinue
$credsOk = $st -and
           -not [string]::IsNullOrWhiteSpace([string]$st.Login) -and
           $st.Password -and ([byte[]]$st.Password).Length -gt 0 -and
           ([string]$st.AutoConnect) -eq 'true'
if (-not $credsOk) {
    $stamp = Join-Path $logDir '.iqconnect-needs-relogin'
    $recent = (Test-Path $stamp) -and ((Get-Item $stamp).LastWriteTime -gt (Get-Date).AddMinutes(-30))
    if (-not $recent) {
        Set-Content -Path $stamp -Value (Get-Date -Format o)
        Add-Content -Path $wlog -Value "$(Get-Date -Format o) KAILANGAN NG OPERATOR: kulang ang naka-save na DTN credentials (Login/Password/AutoConnect). Buksan ang IQCharts, i-type ang password, i-check ang 'Save Login And Password' at 'Automatically Connect', pindutin ang Connect. HINDI ito maaayos ng pag-restart."
    }
    exit 0
}

Add-Content -Path $wlog -Value "$(Get-Date -Format o) IQConnect DOWN habang may $($bridges.Count) bridge -- sinisimulan (may product ID)"
Start-Process -FilePath 'E:\DTN\IQFeed\iqconnect.exe' -WorkingDirectory 'E:\DTN\IQFeed' `
    -ArgumentList '-product','IQFEED_CHARTS','-version','1.8.0.0'
Start-Sleep -Seconds 15
$iq2 = Get-Process iqconnect -ErrorAction SilentlyContinue
if ($iq2) {
    Add-Content -Path $wlog -Value "$(Get-Date -Format o) IQConnect nakabalik pid=$($iq2.Id); ang bridges ay magre-reconnect sa loob ng ~10s"
} else {
    Add-Content -Path $wlog -Value "$(Get-Date -Format o) NABIGO ang pagsisimula ng IQConnect"
}
