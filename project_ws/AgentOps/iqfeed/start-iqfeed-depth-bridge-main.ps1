# IQFeed DEPTH bridge launcher -- untracked kopya na may DB-readiness wait.
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
# BAKIT NASA project_ws (2026-08-23): ang orihinal ay scripts\start-iqfeed-depth-bridge.ps1,
# na TRACKED sa D: tree -- at ang tree na iyon ay nasa Codex branch
# (codex/validate-s16-fixture-checkpoint-20260716). Bawal galawin ang tracked na
# file doon. Ganito rin ang ginawa sa TRADE launcher.
#
# PINAGMUMULAN NG BRIDGE (binago 2026-08-23, hiniling ng operator):
#   E:\dev\wt-window2\scripts\iqfeed_depth_bridge.py  = MAIN, 1,827 linya
# Dati: D:\dev\chili-home-copilot\scripts\iqfeed_depth_bridge.py = 407 linya
# (Codex checkpoint). Ang main ay may #1024 L2 drain barrier, watch-recovery
# quiet window, reader join timeout, sticky resubscribe at breadcrumbs. Ang
# TRADE bridge ay matagal nang tumatakbo mula sa main -- ito ang nagpapantay.
# Ang bagong env vars ay LAHAT may default; ang breadcrumb ay no-op kapag walang
# CHILI_CAPTURED_PAPER_BREADCRUMB_PATH. Ang bridge ay nag-de-derive ng sariling
# repo root mula sa lokasyon nito, kaya self-contained ito sa E: worktree.
#
# PAGBALIK: palitan ang $bridge sa D:\dev\chili-home-copilot\scripts\iqfeed_depth_bridge.py
#
# Logs: D:\CHILI-Docker\chili-data\iqfeed_depth\bridge.log / bridge.err.log

$ErrorActionPreference = 'SilentlyContinue'

$bridge = 'E:\dev\wt-window2\scripts\iqfeed_depth_bridge.py'
$python = 'C:\Users\rindo\miniconda3\envs\chili-env\python.exe'

# 1) IQConnect. KAILANGAN NITO ANG DALAWA (natutunan 2026-08-23):
#    a) credentials sa registry -- kung wala, titigil ito sa GUI login dialog
#       ("Waiting for user or server list") at kailangan ng TAO. Ang DTN ay
#       nagre-require ng relogin tuwing Linggo (tingnan CHILI-sunday-login-reminder).
#    b) -product sa command line -- kung wala, aabot ito sa "0 products left to
#       authenticate. Ready" at TITIGIL doon: hindi kailanman kumokonekta sa quote
#       server, at ang bridges ay makakakita ng "S,SERVER DISCONNECTED" habang
#       mukhang malusog ang lahat.
#    ITINAMA: dati kong isinulat na ang -product IQFEED_CHARTS ang sanhi ng
#    "selected-field acknowledgement mismatch". MALI. Ang mismatch ay dulot ng
#    WALANG upstream. Ang malusog na session 09:38-16:14 ay may 2 authenticated
#    products.
if (-not (Get-Process iqconnect -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath 'E:\DTN\IQFeed\iqconnect.exe' -WorkingDirectory 'E:\DTN\IQFeed' `
        -ArgumentList '-product','IQFEED_CHARTS','-version','1.8.0.0'
    Start-Sleep -Seconds 20
}

# 2) bridge daemon -- laktawan kung may tumatakbo na (idempotent)
# IDEMPOTENCY: tingnan ang PYTHON at ang CMD WRAPPER. May supervisor loop
# ang wrapper (20s na pahinga sa pagitan ng restart); sa loob ng gap na iyon
# ay WALANG python -- kung python lang ang titingnan, makakapaglunsad ang
# Daily task ng PANGALAWANG wrapper at magkakaroon ng dalawang bridge.
$existing = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'cmd.exe'" |
    Where-Object { $_.CommandLine -like '*iqfeed_depth_bridge.py*' -or
                   $_.CommandLine -like '*run-depth-bridge.cmd*' })
if ($existing.Count -gt 0) { exit 0 }

$log = 'D:\CHILI-Docker\chili-data\iqfeed_depth\bridge.log'
$err = 'D:\CHILI-Docker\chili-data\iqfeed_depth\bridge.err.log'

# 2b) HINTAYIN ANG POSTGRES (2026-08-23 reboot race).
#     Sa reboot, ang Logon trigger ay pumuputok AGAD habang nagsisimula pa ang
#     Postgres sa Docker. Bumabagsak ang bridge sa "FATAL: the database system is
#     starting up", lumalabas, tapos nag-e-exit din ang IQConnect dahil nawalan ng
#     client -- at ang natitira ay paikot-ikot sa WinError 10061.
#     Bounded: hanggang 180s, tapos ituloy pa rin (mas mabuti ang malinaw na error
#     sa log kaysa sa tahimik na hindi paglulunsad).
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

if (-not (Test-Path $bridge)) {
    Add-Content -Path $err -Value "$(Get-Date -Format o) LAUNCHER: WALA ang bridge source $bridge -- hindi maglalaunch"
    exit 1
}

# 3) --allow-uncaptured-diagnostic: KAILANGAN ito ng main na bersyon.
#    Ang main ay idinisenyo para sa SEALED capture lane: tumatanggi itong buksan
#    ang provider socket maliban kung may bound na L2 capture handoff
#    (_require_standalone_capture_posture). Ang handoff ay bini-bind IN-PROCESS ng
#    sealed captured-paper supervisor -- WALA nito ang standalone time-share lane.
#    Ang flag ang tanging paraan para tumakbo ito nang standalone.
#
#    ANO ANG NAWAWALA: ang sealed ReplayV3 depth capture mula sa prosesong ito
#    (nagla-log ito ng CRITICAL line -- inaasahan iyon, hindi error). ANO ANG
#    HINDI nawawala: ang mga sulat sa iqfeed_depth_snapshots ay HIWALAY sa capture
#    at tumatakbo pa rin. Ang dating 407-linyang bersyon ay WALA RING capture,
#    kaya ito ay superset -- walang naiwala, nadagdagan ng #1024 L2 drain barrier.
#
#    !! Kung ilalagay ni Codex ang bridge sa ilalim ng sealed lane, ALISIN ang
#    flag -- doon ay ang supervisor na ang magbi-bind ng handoff.
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
#     nawawala lang ang buong ROSS small-cap band sa subscription target. Iyon
#     mismo ang mga pangalang gusto natin sa premarket.
#     Minimal at naka-target: ang database_url LANG ang nawawalang field.
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

Start-Process -FilePath 'E:\dev\wt-window2\project_ws\AgentOps\iqfeed\run-depth-bridge.cmd' -WindowStyle Hidden
