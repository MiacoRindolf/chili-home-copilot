# Diagnose why TCP port 8000 (or -Port) may still show WinError 10048.
# Does not change the system - read the SUMMARY at the end.
#
# From cmd.exe / Cursor "Command Prompt": use scripts\diagnose-port-8000.cmd
#   (running .ps1 directly often opens another window - this terminal stays blank).
# From PowerShell: .\scripts\diagnose-port-8000.ps1  is fine.
param([int]$Port = 8000)

$verdict = "OK"
$reasons = [System.Collections.Generic.List[string]]::new()

function Write-Diag {
    param([string]$Message, [string]$Color = 'White')
    if ($Color -eq 'Cyan') { Write-Host $Message -ForegroundColor Cyan }
    elseif ($Color -eq 'Yellow') { Write-Host $Message -ForegroundColor Yellow }
    elseif ($Color -eq 'Red') { Write-Host $Message -ForegroundColor Red }
    elseif ($Color -eq 'Green') { Write-Host $Message -ForegroundColor Green }
    elseif ($Color -eq 'Gray' -or $Color -eq 'DarkGray') { Write-Host $Message -ForegroundColor DarkGray }
    else { Write-Host $Message -ForegroundColor White }
}

Write-Host ""
Write-Diag "=== Port $Port diagnostics (read-only) ===" 'Cyan'
Write-Diag "This script only reports; it does not kill processes or change reservations." 'DarkGray'
Write-Host ""

# 1) Listeners and owning PIDs (Get-NetTCPConnection)
$hasListener = $false
$hasPid4Listener = $false
$hasOtherListener = $false
$conns = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
if (-not $conns) {
    Write-Diag "Get-NetTCPConnection: no rows for local port $Port." 'Yellow'
} else {
    $listen = $conns | Where-Object { $_.State -eq 'Listen' }
    if (-not $listen) {
        Write-Diag "No LISTEN state on $Port (other states only):" 'Yellow'
        $conns | Format-Table LocalAddress, LocalPort, State, OwningProcess -AutoSize
        $reasons.Add("Nothing in LISTEN on $Port right now; bind error may be timing, another interface, or WSL.")
        $verdict = "CHECK"
    } else {
        $hasListener = $true
        $pids = $listen | Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($pid in $pids) {
            if (-not $pid -or $pid -eq 0) { continue }
            if ($pid -eq 4) {
                $hasPid4Listener = $true
                Write-Diag "OwningProcess: 4 (System) - HTTP.sys / kernel. You cannot taskkill PID 4." 'Red'
                Write-Diag "  Inspect: netsh http show urlacl" 'DarkGray'
                Write-Diag "  Remove reservation (Admin), if listed for this port:" 'DarkGray'
                Write-Diag "  netsh http delete urlacl url=http://+:$Port/" 'DarkGray'
            } else {
                $hasOtherListener = $true
                try {
                    $p = Get-Process -Id $pid -ErrorAction Stop
                    Write-Diag "PID $pid : $($p.ProcessName) - $($p.Path)" 'White'
                } catch {
                    Write-Diag "PID $pid : not visible here - try Admin: taskkill /F /T /PID $pid  or  scripts\reset-chili-dev.cmd" 'Yellow'
                }
            }
        }
        $listen | Format-Table LocalAddress, LocalPort, State, OwningProcess -AutoSize
        if ($hasPid4Listener) {
            $verdict = "ACTION"
            $reasons.Add("PID 4 owns the listener - clear HTTP.sys URL ACL (netsh) or stop the service using HTTP.sys on $Port.")
        }
        if ($hasOtherListener) {
            $verdict = "ACTION"
            $reasons.Add("A normal process is listening - run scripts\reset-chili-dev.cmd (accept UAC) or end that PID in Task Manager.")
        }
    }
}

# 1b) netstat fallback - only LOCAL port $Port (ignore SYN_SENT rows where remote is ...:8000)
Write-Host ""
Write-Diag "--- netstat -ano (local port $Port only) ---" 'Cyan'
$allNet = @(cmd /c "netstat -ano 2>nul")
$netstatLines = @(
    $allNet | Where-Object {
        if ($_ -notmatch '^\s*TCP\s+(\S+)\s+') { return $false }
        $localAddr = $Matches[1]
        $localAddr -match ":${Port}$"
    }
)
if ($netstatLines.Count -gt 0) {
    $netstatLines | ForEach-Object { Write-Host $_ }
    $listening = $netstatLines | Where-Object { $_ -match '\sLISTENING\s+\d+\s*$' }
    if ($listening) {
        if ($verdict -ne 'ACTION') { $verdict = 'ACTION' }
        if (-not ($reasons | Where-Object { $_ -like '*netstat*' })) {
            $reasons.Add("netstat shows LISTENING on local port $Port - use the PID in the last column (Task Manager or taskkill as Admin).")
        }
    } else {
        Write-Diag "(Lines above are not LISTENING - e.g. TIME_WAIT; port may still be in transition.)" 'DarkGray'
    }
} else {
    Write-Diag "(netstat: no rows with LOCAL address ending in :$Port)" 'DarkGray'
}

if (-not $conns -and $verdict -eq 'OK') {
    $verdict = "CHECK"
    $reasons.Add("Windows shows no socket on $Port at this instant. If uvicorn still said 10048: run this script again WHILE the failing server is running, or stop WSL/Docker (wsl --shutdown) and retry.")
}

Write-Host ""
Write-Diag "--- netsh http urlacl (HTTP.sys) ---" 'Cyan'
try {
    $urlacl = netsh http show urlacl 2>$null
    $hit = $urlacl | Select-String -Pattern ":\s*$Port(/|\s|$)"
    if ($hit) {
        $hit | ForEach-Object { Write-Host $_.Line -ForegroundColor Yellow }
        $verdict = "ACTION"
        $reasons.Add("URL ACL mentions port $Port - remove the matching url= line with netsh http delete urlacl (Admin).")
    } else {
        Write-Diag "(No urlacl lines mentioning $Port)" 'DarkGray'
    }
} catch { Write-Diag "Could not run netsh http show urlacl" 'Yellow' }

Write-Host ""
Write-Diag "--- Hyper-V / excluded TCP ranges ---" 'Cyan'
$inRange = $false
try {
    $rangeText = netsh interface ipv4 show excludedportrange protocol=tcp 2>$null
    if ($rangeText) { $rangeText | ForEach-Object { Write-Host $_ } }
    foreach ($line in $rangeText) {
        if ($line -match '^\s*(\d+)\s+(\d+)\s*$') {
            $start = [int]$Matches[1]; $end = [int]$Matches[2]
            if ($Port -ge $start -and $Port -le $end) {
                $inRange = $true
                Write-Diag "Port $Port is INSIDE excluded range $start-$end (Windows reserved)." 'Red'
                $verdict = "ACTION"
                $reasons.Add("Port $Port is in an excluded range - often fixed after reboot or by adjusting Hyper-V/WSL networking; binding can fail with no obvious PID.")
            }
        }
    }
    if (-not $inRange -and $rangeText) {
        Write-Diag "Port $Port is not inside the listed IPv4 excluded ranges." 'Green'
    }
} catch { Write-Diag "Could not read excluded port ranges." 'Yellow' }

# IPv6 excluded ranges (separate table from IPv4)
Write-Host ""
Write-Diag "--- Hyper-V / excluded TCP ranges (IPv6) ---" 'Cyan'
$inRange6 = $false
try {
    $range6 = netsh interface ipv6 show excludedportrange protocol=tcp 2>$null
    if ($range6) { $range6 | ForEach-Object { Write-Host $_ } }
    foreach ($line in $range6) {
        if ($line -match '^\s*(\d+)\s+(\d+)\s*$') {
            $start = [int]$Matches[1]; $end = [int]$Matches[2]
            if ($Port -ge $start -and $Port -le $end) {
                $inRange6 = $true
                Write-Diag "Port $Port is INSIDE IPv6 excluded range $start-$end." 'Red'
                $verdict = "ACTION"
                $reasons.Add("Port $Port is in an IPv6 excluded range - reboot or adjust Hyper-V/WSL networking.")
            }
        }
    }
    if (-not $inRange6 -and $range6) {
        Write-Diag "Port $Port is not inside the listed IPv6 excluded ranges." 'Green'
    }
} catch { Write-Diag "Could not read IPv6 excluded port ranges." 'Yellow' }

Write-Host ""
Write-Diag "--- netsh interface portproxy (WSL / manual forwards) ---" 'Cyan'
try {
    $pp = netsh interface portproxy show all 2>$null
    if ($pp) {
        $ppHit = $pp | Where-Object { $_ -match ":${Port}\b" }
        if ($ppHit) {
            $ppHit | ForEach-Object { Write-Host $_ }
            $verdict = "ACTION"
            $reasons.Add("Port proxy references $Port - list v4tov4 rules; remove with netsh interface portproxy delete (Admin) if unused.")
        } else {
            Write-Diag "(No portproxy lines mentioning $Port)" 'DarkGray'
        }
    } else {
        Write-Diag "(portproxy show all empty or unavailable)" 'DarkGray'
    }
} catch { Write-Diag "Could not read portproxy." 'Yellow' }

Write-Host ""
Write-Diag "--- Test-NetConnection (is anything accepting TCP on port ${Port}?) ---" 'Cyan'
try {
    $tn = Test-NetConnection -ComputerName 127.0.0.1 -Port $Port -WarningAction SilentlyContinue -ErrorAction SilentlyContinue
    if ($null -ne $tn.TcpTestSucceeded) {
        Write-Host "  127.0.0.1:${Port} TcpTestSucceeded = $($tn.TcpTestSucceeded)"
        if ($tn.TcpTestSucceeded) {
            if ($verdict -ne 'ACTION') { $verdict = 'ACTION' }
            $reasons.Add("Something accepted a TCP connection to 127.0.0.1:$Port - a listener exists. Run as Admin: Get-NetTCPConnection -LocalPort $Port -State Listen | Format-Table -AutoSize")
        } else {
            Write-Diag "Nothing accepted TCP to 127.0.0.1:$Port (typical when port is truly free right now)." 'DarkGray'
        }
    }
} catch {
    Write-Diag "Test-NetConnection failed (older OS or policy): $_" 'Yellow'
}

Write-Host ""
Write-Diag "=== SUMMARY ===" 'Cyan'
switch ($verdict) {
    "OK" {
        Write-Diag "Verdict: LOOKS CLEAR for port $Port (no listener, no netstat hit, no urlacl hit, not in excluded ranges, TcpTest failed or not run)." 'Green'
        Write-Diag "If uvicorn still errors with 10048: run this script again while the server process is running, and try: wsl --shutdown" 'DarkGray'
    }
    "CHECK" {
        Write-Diag "Verdict: UNCLEAR - Windows shows no listener and (usually) TcpTest to 127.0.0.1 failed." 'Yellow'
        Write-Diag "That means: right now nothing is accepting TCP on $Port on this machine. 10048 is almost always 'something else already bound 0.0.0.0:$Port when uvicorn started'." 'DarkGray'
        Write-Diag "Next: start uvicorn until you see 10048, leave that window open, run this script again in a second terminal; or run scripts\reset-chili-dev.cmd, then wsl --shutdown, then try again." 'DarkGray'
    }
    "ACTION" {
        Write-Diag "Verdict: ACTION NEEDED - something is (or was) blocking port $Port." 'Red'
    }
}
$seen = @{}
foreach ($r in $reasons) {
    if ($seen.ContainsKey($r)) { continue }
    $seen[$r] = $true
    Write-Host "  * $r"
}

Write-Host ""
Write-Diag "Suggested commands:" 'Cyan'
Write-Host "  scripts\reset-chili-dev.cmd"
Write-Host "  scripts\free-port.cmd -Port $Port"
Write-Host "  wsl --shutdown"
Write-Host "  docker ps   (look for 0.0.0.0:8000->...)"
Write-Host ""
Write-Diag "Diagnostics finished." 'Green'

if ($verdict -eq "ACTION") { exit 2 }
if ($verdict -eq "CHECK") { exit 1 }
exit 0
