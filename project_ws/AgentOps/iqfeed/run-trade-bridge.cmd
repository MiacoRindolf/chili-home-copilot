@echo off
rem ============================================================================
rem  Trade bridge runner (2026-08-23, rev2 pagkatapos ng adversarial audit).
rem
rem  (1) CD SA REPO ROOT -- ANG PINAKAMAHALAGA. Ang app Settings (pydantic) ay
rem      naghahanap ng .env na RELATIVE SA CWD. Kung wala nito, ang CWD ay
rem      C:\Windows\System32 at ang LAHAT ng market-data API key ay NAWAWALA.
rem      SINUKAT 2026-08-23: CWD=C:\ -> massive/polygon/alpaca/ortex LAHAT WALA;
rem      CWD=E:\dev\wt-window2 -> lahat nakatakda. Kapag wala ang mga key, hindi
rem      makakakuha ng movers ang build_equity_universe -> WALANG LAMAN ang ROSS
rem      band -> hindi masu-subscribe ang mga pangalan sa premarket. Ang
rem      DATABASE_URL fix lang ay nagtatakip sa crash, HINDI sa data source.
rem
rem  (2) >> HINDI > . Ang launcher ay sumusulat ng diagnostic (hal. "postgres
rem      hindi handa pagkatapos ng 180s") sa err log BAGO ito ilunsad. Ang `>`
rem      ay pumuputol -- SINUKAT: 0 na "LAUNCHER:" na linya ang nakaligtas. Ang
rem      append ay nagpapanatili rin ng kasaysayan ng restart. May size guard.
rem
rem  (3) Ginagawa ang log dir kung wala -- kung hindi, tahimik na nabibigo ang
rem      redirect at HINDI naglalaunch, exit 0.
rem
rem  BAKIT MAY .CMD: ang Start-Process -RedirectStandard* ng PowerShell ay PIPE
rem  na pinapump ng magulang; sa scheduled task ay lumalabas agad ang magulang,
rem  namamatay ang pump, at bumabara ang bawat sulat sa stderr nang ~1s. Ang
rem  handshake ay lumalampas sa 2.00s ack deadline -> walang katapusang
rem  reconnect, ZERO ticks. SINUKAT: 4.14s -> 0.04s, 28/28 failure -> 0.
rem ============================================================================
set "REPO=E:\dev\wt-window2"
set "DIR=D:\CHILI-Docker\chili-data\iqfeed_trades"
set "LOG=%DIR%\bridge.log"
set "ERR=%DIR%\bridge.err.log"

if not exist "%DIR%" mkdir "%DIR%"

rem  size guard: kung lampas 50MB, i-rotate (isang henerasyon lang)
for %%F in ("%ERR%") do if %%~zF GTR 52428800 move /Y "%ERR%" "%ERR%.old" >nul 2>&1
for %%F in ("%LOG%") do if %%~zF GTR 52428800 move /Y "%LOG%" "%LOG%.old" >nul 2>&1

set "DATABASE_URL=postgresql://chili:chili@localhost:5433/chili"
cd /d "%REPO%"

rem  (4) SUPERVISOR LOOP. Ang _verify_bridge_schema() ay nasa LABAS ng retry
rem      loop ng bridge at wala sa try (main() 3689 vs while 3703), kaya ang
rem      DB na hindi pa handa sa cold boot ay nagiging UNCAUGHT RuntimeError
rem      -> lumalabas ang proseso NANG TULUYAN. Walang nagre-restart: ang
rem      IQConnect watchdog ay lumalabas kapag bridges.Count = 0, at ang
rem      susunod na Daily task ay 03:55/03:56 -- tapos na ang premarket.
rem      Ang bawat restart ay MAY TALA -- hindi tahimik ang pagkamatay.
:bridge_loop
echo %date% %time% [runner] sinisimulan ang trade bridge >>"%ERR%"
"C:\Users\rindo\miniconda3\envs\chili-env\python.exe" "%REPO%\scripts\iqfeed_trade_bridge.py" --allow-uncaptured-diagnostic >>"%LOG%" 2>>"%ERR%"
echo %date% %time% [runner] lumabas rc=%ERRORLEVEL% -- muling sisimulan sa 20s >>"%ERR%"
timeout /t 20 /nobreak >nul 2>&1
goto bridge_loop
