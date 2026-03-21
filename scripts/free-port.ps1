# Stop every process listening on a TCP port (fixes WinError 10048 on Windows).
#
# - Uses Get-NetTCPConnection AND netstat -ano (either can miss the other).
# - Kills listener PID with taskkill /F /T; for uvicorn --reload also targets parent python.
# - Sweeps python.exe processes whose command line looks like uvicorn serving this repo / port.
# - Detects Hyper-V excluded port ranges: if the port is reserved, exits 1 (binding fails with no PID).
# - If still busy, re-launches elevated (UAC). PID 4 = HTTP.sys — see Write-HttpSysHint.
param(
    [int]$Port = 8000
)

function Test-PortInExcludedTcpRange4 {
    param([int]$PortToCheck)
    try {
        $lines = netsh interface ipv4 show excludedportrange protocol=tcp 2>$null
        foreach ($line in $lines) {
            if ($line -match '^\s*(\d+)\s+(\d+)\s*$') {
                $start = [int]$Matches[1]; $end = [int]$Matches[2]
                if ($PortToCheck -ge $start -and $PortToCheck -le $end) {
                    return @{ InRange = $true; Start = $start; End = $end; Family = 'IPv4' }
                }
            }
        }
    } catch { }
    return @{ InRange = $false; Start = 0; End = 0; Family = '' }
}

function Test-PortInExcludedTcpRange6 {
    param([int]$PortToCheck)
    try {
        $lines = netsh interface ipv6 show excludedportrange protocol=tcp 2>$null
        foreach ($line in $lines) {
            if ($line -match '^\s*(\d+)\s+(\d+)\s*$') {
                $start = [int]$Matches[1]; $end = [int]$Matches[2]
                if ($PortToCheck -ge $start -and $PortToCheck -le $end) {
                    return @{ InRange = $true; Start = $start; End = $end; Family = 'IPv6' }
                }
            }
        }
    } catch { }
    return @{ InRange = $false; Start = 0; End = 0; Family = '' }
}

function Get-ExcludedPortMessage {
    param([int]$PortNum)
    $a = Test-PortInExcludedTcpRange4 -PortToCheck $PortNum
    if ($a.InRange) {
        return "Port $PortNum is inside Windows IPv4 EXCLUDED range $($a.Start)-$($a.End) (Hyper-V/WSL). Bind can fail with 10048 even with no listener. Use another port, e.g. 8010: `$env:CHILI_PORT='8010'` then .\scripts\start-dev.ps1"
    }
    $b = Test-PortInExcludedTcpRange6 -PortToCheck $PortNum
    if ($b.InRange) {
        return "Port $PortNum is inside Windows IPv6 EXCLUDED range $($b.Start)-$($b.End). Try another port or reboot."
    }
    return $null
}

function Write-HttpSysHint {
    param([int]$PortNum)
    Write-Host "Port $PortNum is held by System (PID 4) - usually HTTP.sys URL reservation, not a normal app." -ForegroundColor Red
    Write-Host "  Inspect: netsh http show urlacl" -ForegroundColor Yellow
    Write-Host "  If you see http://+:$PortNum/ or similar, remove (run as Admin):" -ForegroundColor Yellow
    Write-Host "  netsh http delete urlacl url=http://+:$PortNum/" -ForegroundColor Gray
}

function Get-ListenerPidsFromNet {
    param([int]$PortNum)
    @(
        Get-NetTCPConnection -LocalPort $PortNum -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    ) | Where-Object { $_ -and $_ -ne 0 }
}

# netstat often still shows a LISTENING row when Get-NetTCPConnection lags or differs (e.g. fast respawn).
function Get-ListenerPidsFromNetstat {
    param([int]$PortNum)
    $set = [System.Collections.Generic.HashSet[int]]::new()
    foreach ($line in @(cmd /c "netstat -ano 2>nul")) {
        if ($line -notmatch 'LISTENING') { continue }
        if ($line -notmatch '^\s*TCP\s+(\S+)\s+') { continue }
        $localAddr = $Matches[1]
        if ($localAddr -notmatch ":${PortNum}$") { continue }
        if ($line -match 'LISTENING\s+(\d+)\s*$') {
            $pidVal = [int]$Matches[1]
            if ($pidVal -gt 0) { [void]$set.Add($pidVal) }
        }
    }
    @($set)
}

function Get-AllListenerPids {
    param([int]$PortNum)
    $u = [System.Collections.Generic.HashSet[int]]::new()
    foreach ($x in (Get-ListenerPidsFromNet -PortNum $PortNum)) { [void]$u.Add($x) }
    foreach ($x in (Get-ListenerPidsFromNetstat -PortNum $PortNum)) { [void]$u.Add($x) }
    @($u)
}

function Test-AnyListenerOnPort {
    param([int]$PortNum)
    $p = Get-AllListenerPids -PortNum $PortNum
    return ($null -ne $p -and $p.Count -gt 0)
}

function Stop-ProcessTree {
    param([int]$ProcId)
    if ($ProcId -le 4) { return }
    cmd /c "taskkill /F /T /PID $ProcId 2>nul" | Out-Null
    Stop-Process -Id $ProcId -Force -ErrorAction SilentlyContinue
}

# Kill PIDs holding the socket; prefer killing parent python when child is worker (uvicorn --reload).
function Stop-ListenerProcessTrees {
    param([int]$PortNum)

    $listenerPids = Get-AllListenerPids -PortNum $PortNum
    if (-not $listenerPids) { return }

    $roots = [System.Collections.Generic.HashSet[int]]::new()
    foreach ($lp in $listenerPids) {
        if ($lp -eq 4) { continue }
        $killPid = $lp
        try {
            $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $lp" -ErrorAction Stop
            $pp = [int]$cim.ParentProcessId
            if ($pp -gt 4) {
                try {
                    $parentName = (Get-Process -Id $pp -ErrorAction Stop).ProcessName
                    if ($parentName -match '^(python|pythonw|python\d|py)$') {
                        $killPid = $pp
                    }
                } catch {
                    $killPid = $pp
                }
            }
        } catch { }
        [void]$roots.Add($killPid)
    }

    foreach ($procId in $roots) {
        if ($procId -eq 4) { continue }
        try {
            $p = Get-Process -Id $procId -ErrorAction Stop
            Write-Host "Stopping PID $procId ($($p.ProcessName)) on port $PortNum (process tree)..." -ForegroundColor Yellow
        } catch {
            Write-Host "Stopping PID $procId on port $PortNum (process tree)..." -ForegroundColor Yellow
        }
        Stop-ProcessTree -ProcId $procId
    }

    Start-Sleep -Milliseconds 400
    foreach ($lp in (Get-AllListenerPids -PortNum $PortNum)) {
        if ($lp -eq 4) { continue }
        Write-Host "Stopping leftover listener PID $lp..." -ForegroundColor Yellow
        Stop-ProcessTree -ProcId $lp
    }
}

# Last resort: python.exe running uvicorn app.main:app and this port (--port, host:port, or default 8000).
function Stop-ChiliUvicornProcesses {
    param([int]$PortNum)
    $portS = [string]$PortNum
    $esc = [regex]::Escape($portS)
    try {
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {
            $cmd = $_.CommandLine
            if (-not $cmd) { return }
            if ($_.Name -notmatch '^python(\d)?w?\.exe$') { return }
            if ($cmd -notmatch 'uvicorn') { return }
            if ($cmd -notmatch 'app\.main:app') { return }
            $hitPort = $false
            if ($cmd -match "(?:^|\s)--port\s*=\s*$esc(?:\s|$)") { $hitPort = $true }
            if ($cmd -match "(?:^|\s)--port\s+$esc(?:\s|$)") { $hitPort = $true }
            if ($cmd -match (':' + $esc + '(?:\s|$|,|\))')) { $hitPort = $true }
            if (-not $hitPort -and $PortNum -eq 8000 -and $cmd -notmatch '(?:^|\s)--port') { $hitPort = $true }
            if (-not $hitPort) { return }
            $pidVal = [int]$_.ProcessId
            if ($pidVal -le 4) { return }
            Write-Host "Stopping CHILI uvicorn PID $pidVal (command-line match)..." -ForegroundColor Yellow
            Stop-ProcessTree -ProcId $pidVal
        }
    } catch { }
}

# --- Phase A: repeated aggressive clear (no admin) ---
for ($round = 0; $round -lt 8; $round++) {
    if (-not (Test-AnyListenerOnPort -PortNum $Port)) { break }
    Stop-ListenerProcessTrees -PortNum $Port
    Stop-ChiliUvicornProcesses -PortNum $Port
    Start-Sleep -Milliseconds 450
}

$exMsg = Get-ExcludedPortMessage -PortNum $Port

if (-not (Test-AnyListenerOnPort -PortNum $Port)) {
    if ($exMsg) {
        Write-Host $exMsg -ForegroundColor Red
        Write-Host "Run: .\scripts\diagnose-port-8000.ps1 -Port $Port" -ForegroundColor Cyan
        exit 1
    }
    # Re-check after short wait; sometimes socket release is delayed (avoids 10048 right after "Port is free").
    Start-Sleep -Milliseconds 600
    if (Test-AnyListenerOnPort -PortNum $Port) {
        Stop-ListenerProcessTrees -PortNum $Port
        Stop-ChiliUvicornProcesses -PortNum $Port
        Start-Sleep -Milliseconds 500
    }
    if (-not (Test-AnyListenerOnPort -PortNum $Port)) {
        Write-Host "Port $Port is free." -ForegroundColor Green
        exit 0
    }
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

$pids = Get-AllListenerPids -PortNum $Port
$hadPid4 = $false
foreach ($procId in $pids) {
    if ($procId -eq 4) {
        Write-HttpSysHint -PortNum $Port
        $hadPid4 = $true
    }
}

if (-not $isAdmin) {
    Write-Host "Port $Port still in use after aggressive cleanup. Requesting Administrator (UAC)..." -ForegroundColor Yellow
    $args = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Port $Port"
    $proc = Start-Process powershell -Verb RunAs -ArgumentList $args -Wait -PassThru
    $code = $proc.ExitCode
    if ($code -eq 0) {
        Write-Host "Elevated free-port finished: SUCCESS (exit 0). Port $Port should be free." -ForegroundColor Green
    } else {
        Write-Host "Elevated free-port finished: FAILED (exit $code)." -ForegroundColor Red
        if ($exMsg) { Write-Host $exMsg -ForegroundColor Red }
        Write-Host "Next: .\scripts\diagnose-port-8000.ps1 -Port $Port" -ForegroundColor Cyan
    }
    exit $code
}

# --- Elevated path ---
for ($round = 0; $round -lt 5; $round++) {
    Stop-ListenerProcessTrees -PortNum $Port
    Stop-ChiliUvicornProcesses -PortNum $Port
    Start-Sleep -Milliseconds 500
    if (-not (Test-AnyListenerOnPort -PortNum $Port)) { break }
}

if ($hadPid4) {
    $still4 = @(Get-AllListenerPids -PortNum $Port) -contains 4
    if ($still4) {
        Write-Host "Port $Port still owned by PID 4. Fix HTTP.sys urlacl or change port." -ForegroundColor Red
        exit 1
    }
}

Start-Sleep -Seconds 1
if (-not (Test-AnyListenerOnPort -PortNum $Port)) {
    if ($exMsg) {
        Write-Host $exMsg -ForegroundColor Red
        exit 1
    }
    Write-Host "Port $Port is now free." -ForegroundColor Green
    exit 0
}

$still = Get-AllListenerPids -PortNum $Port
Write-Host "Port $Port still in use after kill attempts." -ForegroundColor Red
foreach ($sp in $still) {
    if ($sp -eq 4) {
        Write-HttpSysHint -PortNum $Port
    } else {
        try {
            $p2 = Get-Process -Id $sp -ErrorAction Stop
            Write-Host "  Still listening: PID $sp ($($p2.ProcessName))" -ForegroundColor Yellow
        } catch {
            Write-Host "  Still listening: PID $sp" -ForegroundColor Yellow
        }
    }
}
if ($exMsg) { Write-Host $exMsg -ForegroundColor Red }
Write-Host "If using WSL/Docker: wsl --shutdown  or stop containers publishing :$Port" -ForegroundColor Gray
Write-Host "Run: .\scripts\diagnose-port-8000.ps1 -Port $Port" -ForegroundColor Cyan
exit 1
