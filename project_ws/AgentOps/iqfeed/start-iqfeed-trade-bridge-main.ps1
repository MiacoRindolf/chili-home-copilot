# CHILI IQFeed TRADE bridge launcher — MAIN-lineage cutover (2026-08-17, Claude).
# ── ISANG-ROOT NA CONSOLIDATION (2026-08-24) ────────────────────────────────
# Ang buong launch chain ay dapat nasa ILALIM NG IISANG ROOT, dahil ang
# ``scripts/collect_captured_paper_host_snapshot.py`` ay nagpapatunay ng TATLONG
# path laban sa isang ``--legacy-root``: ang VBS wrapper, ang PowerShell starter
# na ito, at ang bridge .py. Dati itong hati sa tatlong drive (wscript sa C:,
# .vbs/.ps1 sa D:, bridge .py sa E:), kaya ang collector ay tumatanggi nang
# PATH_OUTSIDE_ROOT at ang capture-host cutover ay walang rollback authority --
# kaya nananatiling hindi nakabuklod ang sealed capture rail (5,302,433 na row
# ang tinanggihan noong 2026-08-24).
#
# Ang root ngayon ay E:\dev\wt-window2, kung saan na tumatakbo ang bridges.
# Ang ``run-hidden.vbs`` ay naka-track na sa ``scripts/`` ng repo na ito at
# byte-identikal sa lumang kopya sa D:.
#
# ⚠️ Ang kopya sa D:\dev\chili-home-copilot ay iniwan nang buo bilang fallback.
#
# BAKIT ITO UMIIRAL: ang dating launcher (scripts\start-iqfeed-trade-bridge.ps1 sa
# D: checkout) ay nagpapatakbo ng bridge mula sa D:\dev\chili-home-copilot — isang
# LUMANG snapshot (<=2026-07-11, Codex branch) na WALANG subscribe-hint fast poll
# (momentum_bridge_subscribe_requests), walang capacity env levers na kumpleto, at
# wala ang R6/R7 hint-resilience fixes. Ang wrapper na ito ay:
#   1. Nagse-set ng Tier-1 capacity env levers (2026-08-04 + 2026-08-17 reports):
#      HARD_MAX=480/FLOOR=400 (iwas sa ~500 provider ceiling at sa 21-oras na
#      recovery ramp pagkatapos ng halving), FRESH_WINDOW=600s (hints buhay nang
#      mas matagal kaysa sa admission race), ELIGIBLE_FRESH=3600s.
#   2. Nagpapatakbo ng bridge mula sa E:\dev\wt-window2 (laging naka-reset sa
#      pinakabagong main) — kaya kasama ang hint fast-poll + R6/R7.
# HINDI ginagalaw ang D: tree (may Codex work doon). Ang scheduled task
# CHILI-IQFeed-Trade-Bridge-Daily ay nakaturo na rito.
# Logs: parehong lugar (D:\CHILI-Docker\chili-data\iqfeed_trades\bridge.log).

$ErrorActionPreference = 'SilentlyContinue'
$bridge = 'E:\dev\wt-window2\scripts\iqfeed_trade_bridge.py'
$python = 'C:\Users\rindo\miniconda3\envs\chili-env\python.exe'

# 1) IQConnect (auto-login sa saved credentials; ang CHARTS auto-login ang tamang
#    manual relogin kapag na-logout — tingnan ang memory notes)
if (-not (Get-Process iqconnect -ErrorAction SilentlyContinue)) {
    # -product ay KAILANGAN: kung wala, aabot lang ito sa "0 products left to
    # authenticate. Ready" at hindi kailanman kumokonekta sa quote server --
    # tahimik na S,SERVER DISCONNECTED. Tingnan ang memory:
    # reference_iqconnect_needs_credentials_and_product.
    Start-Process -FilePath 'E:\DTN\IQFeed\iqconnect.exe' -WorkingDirectory 'E:\DTN\IQFeed' `
        -ArgumentList '-product','IQFEED_CHARTS','-version','1.8.0.0'
    Start-Sleep -Seconds 20
}

# 2) trade-bridge daemon — laktawan kung may tumatakbo na (alinmang kopya)
# IDEMPOTENCY: tingnan ang PYTHON at ang CMD WRAPPER. May supervisor loop
# ang wrapper (20s na pahinga sa pagitan ng restart); sa loob ng gap na iyon
# ay WALANG python -- kung python lang ang titingnan, makakapaglunsad ang
# Daily task ng PANGALAWANG wrapper at magkakaroon ng dalawang bridge.
$existing = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'cmd.exe'" |
    Where-Object { $_.CommandLine -like '*iqfeed_trade_bridge.py*' -or
                   $_.CommandLine -like '*run-trade-bridge.cmd*' })
if ($existing.Count -gt 0) { exit 0 }

# 3) Tier-1 capacity env levers (namamana ng child python process)
$env:IQFEED_WATCH_HARD_MAX = '480'
$env:IQFEED_WATCH_FLOOR = '400'
$env:IQFEED_SUBSCRIBE_FRESH_WINDOW_S = '600'
$env:IQFEED_ELIGIBLE_FRESH_SECONDS = '3600'
# CATCHUP (2026-08-27): ang knob na ito ay dating itinakda LAMANG sa env ng isang
# buhay na proseso at nawala sa 03:56 na restart -- kinabukasan mismo ay bumagsak
# ang frontier nang ~50 MINUTO sa open habang default 2048 ang drain. Sinukat
# 2026-08-24: sa 30-49k rows/min na open ay lumalayo ang frontier nang 0.35-0.66
# s/s. Ceiling: CATCHUP_BATCH_EVENTS x 18 < 65,535 => max 3,640; ang 3600 ang
# napatunayang halaga. Ang isang lever na hindi naka-persist ay hindi lever.
$env:IQFEED_DB_RELEASE_CATCHUP_BATCH_EVENTS = '3600'

$dir = 'D:\CHILI-Docker\chili-data\iqfeed_trades'
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
$log = Join-Path $dir 'bridge.log'
$err = Join-Path $dir 'bridge.err.log'
# --allow-uncaptured-diagnostic: ang main bridge ay dinisenyong tumakbo sa ilalim
# ng sealed capture host; sa STANDALONE (ordinary time-share) mode, ang flag na ito
# ang tanging daan — ang epekto ay WALANG ReplayV3 certification mula sa prosesong
# ito (hindi kailangan ng ordinary lane; ang sealed service ay may sariling host).
# Ang tape/hints/DB writes ay normal.
# v2 (08-18): -WorkingDirectory idinagdag — kung wala, ang CWD ng child ay
# System32 kaya hindi mahanap ng pydantic Settings() ang E:\.env
# (database_url Field required -> "ross universe query failed" /
# "subscription coverage unavailable" sa bridge.err.log buong araw 08-18).
# 3b) HINTAYIN ANG POSTGRES BAGO ILUNSAD ANG BRIDGE (2026-08-23 reboot race).
#     Sa reboot, ang Logon trigger ay pumuputok AGAD habang nagsisimula pa ang
#     Postgres sa Docker. Ang bridge ay may read-only schema verification sa
#     startup; kapag hindi pa handa ang DB ito ay bumabagsak sa "FATAL: the
#     database system is starting up" -> RuntimeError -> exit. Tapos nag-e-exit
#     din ang IQConnect dahil nawalan ito ng client, at ang bridges ay
#     paikot-ikot sa WinError 10061 nang WALANG magre-restart ng IQConnect.
#     Nangyari ito nang eksakto noong 2026-08-23 17:54 at kinailangan ng manual
#     na pag-restart. Bounded: hanggang 180s, tapos ituloy pa rin (mas mabuti ang
#     malinaw na error sa log kaysa sa tahimik na hindi paglulunsad).
$dbProbe = @'
import psycopg2, sys
try:
    psycopg2.connect("postgresql://chili:chili@localhost:5433/chili", connect_timeout=3).close()
    sys.exit(0)
except Exception:
    sys.exit(1)
'@
$dbReady = $false
for ($i = 0; $i -lt 60; $i++) {
    $dbProbe | & $python - 2>$null
    if ($LASTEXITCODE -eq 0) { $dbReady = $true; break }
    Start-Sleep -Seconds 3
}
if (-not $dbReady) {
    Add-Content -Path $err -Value "$(Get-Date -Format o) LAUNCHER: postgres hindi handa pagkatapos ng 180s; itutuloy pa rin"
}

# 2c) DATABASE_URL para sa ANAK NA PROSESO (2026-08-23).
#     Ang bridge ay may sariling hardcoded fallback para sa SARILING nitong
#     koneksyon, kaya gumagana ang mga sulat nito. PERO kapag ini-import nito ang
#     app.services.trading.momentum_neural.universe para sa ROSS band, hinihila
#     nito ang app Settings -- na nangangailangan ng database_url mula sa env o
#     .env. Wala sa dalawa ang naaabot ng schtasks, kaya:
#         pydantic: database_url  Field required
#     at ang ROSS source ay TAHIMIK na bumabagsak sa
#         "subscription coverage unavailable ... source=ross code=ross_query_failed"
#     Fail-open ang disenyo (pinapanatili ang prior target), kaya WALANG crash --
#     nawawala lang ang buong ROSS band sa subscription target. Iyon mismo ang mga
#     pangalang gusto natin sa premarket.
$env:DATABASE_URL = 'postgresql://chili:chili@localhost:5433/chili'

# 3) PAGLULUNSAD SA PAMAMAGITAN NG .CMD (2026-08-23) -- HUWAG ibalik sa
#    Start-Process -RedirectStandardOutput/-RedirectStandardError.
#    Ang PowerShell redirect ay PIPE na pinapump ng MAGULANG. Sa scheduled task
#    ay agad lumalabas ang PowerShell, namamatay ang pump, napupuno ang pipe, at
#    BUMABARA ang bawat sulat sa stderr nang ~0.5-1.5s.
#    SINUKAT: sa ilalim ng lumang launcher ay 4.14s (p50) bago ma-proseso ang
#    handshake at 28/28 generation ang nabigo sa 2.00s deadline
#    (SELECTED_FIELDS_ACK_TIMEOUT_S, PR #1024) -> walang katapusang reconnect,
#    ZERO ticks. Sa .cmd: 0.04s hanggang READY, 0 failure, generation=1.
#    Ang > at 2> ng cmd ay TUNAY na file handle -- walang pump, hindi bumabara.

# PARITY SA DEPTH (2026-08-24): mabilis na kabiguan kapag wala ang bridge source.
# Pinapangalanan din nito ang BUONG path ng bridge sa starter, na siyang
# hinahanap ng host-snapshot collector para patunayan na ang starter at ang
# tumatakbong proseso ay tumutukoy sa IISANG script
# (`wrapper_target_matches_running_process`).
if (-not (Test-Path $bridge)) {
    Add-Content -Path $err -Value "$(Get-Date -Format o) LAUNCHER: WALA ang bridge source $bridge -- hindi maglalaunch"
    exit 1
}

Start-Process -FilePath 'E:\dev\wt-window2\project_ws\AgentOps\iqfeed\run-trade-bridge.cmd' -WindowStyle Hidden
